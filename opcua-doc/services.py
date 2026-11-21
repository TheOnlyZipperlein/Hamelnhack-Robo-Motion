"""Read-only OPC UA metadata adapter for the installed ``asyncua`` (2.1.0).

Bounded, *sequential* read surface over an ``asyncua.client.Client``: paged
child browsing, per-node metadata reads, and method-signature extraction.
No writes/subscriptions/plant ops. Every wire request is locked, throttled,
timed out, and bounded by a request budget + scan deadline (exhaustion raises
``scan.ScanLimit``), with per-request timeouts clamped to the remaining scan
time. Continuation-point cleanup is best-effort through the same
budget/throttle/deadline path; the session close is the fallback. Per-node OPC
failures are controlled and recorded as sanitized text (never server raw
payloads) into the shared :attr:`MetadataService.issues` list.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from asyncua import ua
from asyncua.client import Client

from scan import ScanLimit

# Spec ValueRank for a one-dimensional array (OPC UA Part 3): 1 = OneDimension
# (vs -1 Scalar, -2 Any, -3 ScalarOrOneDimension, 0 OneOrMoreDimensions).
# A method signature property must be exactly one-dimensional, so rank 1
# alone is accepted; the looser ranks are not.
_ONE_DIMENSION_RANK = 1

# ns0 BrowseNames accepted as a method-signature property.
_ARG_NAMES = frozenset({"InputArguments", "OutputArguments"})

# The five standard OPC UA Argument fields a decoded signature entry must
# carry. asyncua decodes wire values into its auto-generated struct class
# (``uaprotocol_auto.Argument``), which is NOT the hand-written ``ua.Argument``
# alias, so entries are validated by shape, not by class identity.
_ARG_FIELDS = ("Name", "DataType", "ValueRank", "ArrayDimensions", "Description")

# Allowlisted metadata attributes -> snake_case key + expected wire VariantType.
_ATTR_KEYS: dict[ua.AttributeIds, str] = {
    ua.AttributeIds.Description: "description",
    ua.AttributeIds.DisplayName: "display_name",
    ua.AttributeIds.DataType: "data_type",
    ua.AttributeIds.ValueRank: "value_rank",
    ua.AttributeIds.AccessLevel: "access_level",
    ua.AttributeIds.UserAccessLevel: "user_access_level",
}
_EXPECTED_VARIANT: dict[ua.AttributeIds, ua.VariantType] = {
    ua.AttributeIds.Description: ua.VariantType.LocalizedText,
    ua.AttributeIds.DisplayName: ua.VariantType.LocalizedText,
    ua.AttributeIds.DataType: ua.VariantType.NodeId,
    ua.AttributeIds.ValueRank: ua.VariantType.Int32,
    ua.AttributeIds.AccessLevel: ua.VariantType.Byte,
    ua.AttributeIds.UserAccessLevel: ua.VariantType.Byte,
}


def _sanitize(exc: BaseException) -> str:
    """Reduce an exception to safe text; never embed server-provided payloads."""
    if isinstance(exc, ua.UaStatusCodeError):
        sc = ua.StatusCode(exc.code)
        return f"{sc.name}(0x{(exc.code & 0xFFFFFFFF):08X})"
    if isinstance(exc, asyncio.TimeoutError):
        return "request-timeout"
    if isinstance(exc, ScanLimit):
        return "scan-limit"
    return type(exc).__name__


class MetadataServiceError(Exception):
    """Controlled per-node failure. ``str()`` carries sanitized text only."""


class _CardinalityError(Exception):
    """Internal: a response carried a different value count than requested."""


class _TypeMismatchError(Exception):
    """Internal: an attribute arrived with an unexpected type."""


class MetadataService:
    """Bounded, sequential, read-only OPC UA metadata accessor."""

    def __init__(
        self,
        client: Client,
        *,
        max_requests: int,
        page_size: int,
        request_interval: float,
        timeout: float,
        scan_timeout: float,
    ) -> None:
        self._client = client
        self._ua = client.uaclient
        self._max_requests = max(1, int(max_requests))
        self._page_size = max(1, int(page_size))
        self._interval = max(0.0, float(request_interval))
        self._timeout = float(timeout)
        self._deadline = time.monotonic() + float(scan_timeout)
        self._requests_left = self._max_requests
        self._failed_attempts = 0
        self._last_ts: float | None = None
        self._lock = asyncio.Lock()
        self.issues: list[str] = []

    def _record_failure(self, ctx: str, exc: BaseException) -> None:
        self._failed_attempts += 1
        self.issues.append(f"{ctx}: {_sanitize(exc)}")

    async def _acquire(self) -> None:
        """Reserve one request slot; enforce budget, throttle, then deadline.

        The deadline is checked up front (to avoid a pointless throttle sleep)
        and re-checked *after* the throttle sleep, so a request is never
        admitted once the scan window has closed. The throttle sleep itself is
        clamped to the time remaining on the deadline, so a finite
        ``request_interval`` can never push the scan past its window even when
        no request ends up being sent. Raises ScanLimit.
        """
        if time.monotonic() >= self._deadline:
            raise ScanLimit("scan deadline exceeded")
        if self._requests_left <= 0:
            raise ScanLimit("request budget exhausted")
        if self._last_ts is not None:
            gap = self._interval - (time.monotonic() - self._last_ts)
            if gap > 0:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise ScanLimit("scan deadline exceeded")
                await asyncio.sleep(min(gap, remaining))
        if time.monotonic() >= self._deadline:
            raise ScanLimit("scan deadline exceeded")
        self._requests_left -= 1
        self._last_ts = time.monotonic()

    async def _guarded(self, ctx: str, factory) -> Any:
        """Run one wire request: locked, throttled, deadline-clamped, sanitized.

        ``factory`` is a zero-arg callable returning the request coroutine. It
        is invoked only *after* a slot is acquired, so a request aborted by
        budget/deadline never leaves an unawaited RPC coroutine behind. The
        per-request timeout is clamped to the time remaining on the scan
        deadline. Errors are sanitized; ScanLimit propagates untouched.
        """
        async with self._lock:
            await self._acquire()  # ScanLimit propagates (budget/deadline).
            remaining = self._deadline - time.monotonic()
            bound = min(self._timeout, remaining)  # never run past the deadline
            try:
                return await asyncio.wait_for(factory(), bound)
            except (asyncio.TimeoutError, ua.UaError, ConnectionError, OSError) as exc:
                self._record_failure(ctx, exc)
                raise MetadataServiceError(_sanitize(exc)) from None

    def _check_browse_result(self, ctx: str, result: ua.BrowseResult) -> ua.BrowseResult:
        """Raise a controlled failure on a bad per-operation result status.

        A bad (or uncertain) ``BrowseResult.StatusCode`` means this page is
        unusable; it is recorded and surfaced as ``MetadataServiceError``
        instead of being silently trusted.
        """
        st = result.StatusCode
        if st is not None and not st.is_good():
            err = ua.UaStatusCodeError(st.value)
            self._record_failure(ctx, err)
            raise MetadataServiceError(_sanitize(err)) from None
        return result

    @staticmethod
    def _to_nodeid(node_id: ua.NodeId | int | str) -> ua.NodeId:
        if isinstance(node_id, ua.NodeId):
            return node_id
        if isinstance(node_id, int):
            return ua.NodeId(node_id, 0)
        return ua.NodeId.from_string(node_id)

    @staticmethod
    def _read_params(items: list[ua.ReadValueId]) -> ua.ReadParameters:
        p = ua.ReadParameters()
        p.MaxAge = 0
        p.TimestampsToReturn = ua.TimestampsToReturn.Neither
        p.NodesToRead = items
        return p

    @staticmethod
    def _rv(node_id: ua.NodeId, attr: ua.AttributeIds) -> ua.ReadValueId:
        return ua.ReadValueId(
            NodeId=node_id,
            AttributeId=attr,  # explicit, never the default 0
            IndexRange=None,
            DataEncoding=ua.QualifiedName(),
        )

    async def _browse_first(self, node_id: ua.NodeId) -> ua.BrowseResult:
        ctx = f"browse {node_id.to_string()}"
        desc = ua.BrowseDescription()
        desc.NodeId = node_id
        desc.BrowseDirection = ua.BrowseDirection.Forward
        desc.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences, 0)
        desc.IncludeSubtypes = True
        desc.NodeClassMask = 0  # all node classes
        desc.ResultMask = ua.BrowseResultMask.All
        params = ua.BrowseParameters()
        params.View = ua.ViewDescription()
        params.RequestedMaxReferencesPerNode = self._page_size  # bounded page
        params.NodesToBrowse = [desc]
        results = await self._guarded(ctx, lambda: self._ua.browse(params))
        return self._check_browse_result(ctx, results[0])

    async def _browse_next(self, cont: bytes) -> ua.BrowseResult:
        ctx = "browse-next"
        params = ua.BrowseNextParameters()
        params.ReleaseContinuationPoints = False
        params.ContinuationPoints = [cont]
        results = await self._guarded(ctx, lambda: self._ua.browse_next(params))
        return self._check_browse_result(ctx, results[0])

    async def _release(self, cont: bytes) -> None:
        """Best-effort continuation release over the common request path.

        Shares the request budget, throttle, and deadline with normal requests
        (via :meth:`_guarded`). When no slot is available (budget/deadline
        exhausted) or the request fails, the release is skipped and reported;
        the session close is the fallback that frees the server-side
        continuation point. Never raises, so it cannot mask a scan result.
        """
        params = ua.BrowseNextParameters()
        params.ReleaseContinuationPoints = True
        params.ContinuationPoints = [cont]
        try:
            await self._guarded("continuation-release", lambda: self._ua.browse_next(params))
        except Exception:
            # ScanLimit (slot unavailable) and MetadataServiceError (RPC
            # failure, already recorded by _guarded) are both non-fatal here.
            self.issues.append("continuation-release: best-effort-skipped")

    async def browse_children(self, node_id: ua.NodeId | int | str) -> AsyncIterator[ua.ReferenceDescription]:
        """Yield forward HierarchicalReferences (subtypes, all classes, full mask)."""
        nid = self._to_nodeid(node_id)
        cont: bytes | None = None
        try:
            while True:
                result = await self._browse_first(nid) if cont is None else await self._browse_next(cont)
                # Save the page's continuation *before* yielding it: on early
                # close the generator suspends at a yield, so ``cont`` must
                # already hold the current page's point for the finally-block
                # release. ``cont`` is only advanced on success, so it always
                # marks the frontier of unfetched pages.
                cont = result.ContinuationPoint
                for ref in result.References or []:
                    yield ref
                if cont is None:
                    break  # empty continuation terminates the page walk
        finally:
            if cont is not None:
                await self._release(cont)

    async def metadata(self, node_id: ua.NodeId | int | str, node_class: ua.NodeClass) -> dict[str, Any]:
        """One Read batch over the node-class allowlist; snake_case keys.

        Values are JSON-safe primitives (str/int/None); unreadable attributes
        arrive as ``{"status": str, "value": None}`` markers. A response with
        a different value count than requested fails closed (raises
        ``MetadataServiceError``) instead of silently truncating the mapping.
        """
        nid = self._to_nodeid(node_id)
        attrs = [ua.AttributeIds.Description, ua.AttributeIds.DisplayName]
        if node_class in (ua.NodeClass.Variable, ua.NodeClass.VariableType):
            attrs += [ua.AttributeIds.DataType, ua.AttributeIds.ValueRank]
        if node_class == ua.NodeClass.Variable:
            attrs += [ua.AttributeIds.AccessLevel, ua.AttributeIds.UserAccessLevel]

        ctx = f"metadata {nid.to_string()}"
        params = self._read_params([self._rv(nid, a) for a in attrs])
        values = await self._guarded(ctx, lambda: self._ua.read(params))

        if len(values) != len(attrs):
            # Wrong response cardinality: never silently truncate or pad.
            err = _CardinalityError()
            self._record_failure(ctx, err)
            raise MetadataServiceError(_sanitize(err)) from None

        out: dict[str, Any] = {}
        for attr, dv in zip(attrs, values):
            out[_ATTR_KEYS[attr]] = _typed_value(attr, dv)
        return out

    async def identify(self, node_id: ua.NodeId | int | str) -> tuple[ua.NodeClass, str, str | None]:
        """One Read batch identifying an arbitrary node (custom scan root).

        Returns ``(node_class, browse_name, display_name)``. Used only to seed
        a user-selected scan root; the standardized root needs no
        identification. Fails closed (``MetadataServiceError``) on any bad,
        null, or type-mismatched attribute, so an unusable root aborts the run
        instead of producing a mis-labelled document.
        """
        nid = self._to_nodeid(node_id)
        ctx = f"identify {nid.to_string()}"
        params = self._read_params([
            self._rv(nid, ua.AttributeIds.NodeClass),
            self._rv(nid, ua.AttributeIds.BrowseName),
            self._rv(nid, ua.AttributeIds.DisplayName),
        ])
        values = await self._guarded(ctx, lambda: self._ua.read(params))
        if len(values) != 3:  # wrong cardinality: fail closed
            self._record_failure(ctx, _CardinalityError())
            raise MetadataServiceError(_sanitize(_CardinalityError())) from None
        nc_dv, bn_dv, dn_dv = values

        if not _good(nc_dv) or nc_dv.Value is None:
            self._record_failure(ctx, nc_dv.StatusCode)
            raise MetadataServiceError(_sanitize(nc_dv.StatusCode)) from None
        nc = nc_dv.Value.Value  # Variant payload, one level below the DataValue
        if isinstance(nc, ua.NodeClass):
            node_class = nc
        elif isinstance(nc, int) and not isinstance(nc, bool):
            node_class = ua.NodeClass(nc)
        else:
            self._record_failure(ctx, _TypeMismatchError())
            raise MetadataServiceError(_sanitize(_TypeMismatchError())) from None

        if (
            not _good(bn_dv)
            or bn_dv.Value is None
            or bn_dv.Value.VariantType != ua.VariantType.QualifiedName
            or bn_dv.Value.Value is None
        ):
            self._record_failure(ctx, _TypeMismatchError())
            raise MetadataServiceError(_sanitize(_TypeMismatchError())) from None
        browse_name = bn_dv.Value.Value.Name

        if (
            not _good(dn_dv)
            or dn_dv.Value is None
            or dn_dv.Value.VariantType != ua.VariantType.LocalizedText
            or dn_dv.Value.Value is None
        ):
            self._record_failure(ctx, _TypeMismatchError())
            raise MetadataServiceError(_sanitize(_TypeMismatchError())) from None
        display_name = dn_dv.Value.Value.Text

        return node_class, browse_name, display_name

    def _reject(self, ref: ua.ReferenceDescription, reason: str) -> None:
        self.issues.append(f"arguments rejected [{reason}] {ref.BrowseName.to_string()}")
        return None

    async def arguments(self, ref: ua.ReferenceDescription) -> list[dict[str, Any]] | None:
        """Extract Input/OutputArguments; fail closed (return None) on any mismatch."""
        # 1. Strict local standard HasProperty forward ref, ns0 exact name,
        #    Variable node class + standard PropertyType.
        if not ref.IsForward:
            return self._reject(ref, "not-forward")
        if ref.ReferenceTypeId != ua.NodeId(ua.ObjectIds.HasProperty, 0):
            return self._reject(ref, "not-hasproperty")
        bn = ref.BrowseName
        if bn.NamespaceIndex != 0 or bn.Name not in _ARG_NAMES:
            return self._reject(ref, "bad-browse-name")
        if ref.NodeClass != ua.NodeClass.Variable:
            return self._reject(ref, "not-variable")
        if ref.TypeDefinition != ua.NodeId(ua.ObjectIds.PropertyType, 0):
            return self._reject(ref, "not-property-type")

        target = ref.NodeId  # ExpandedNodeId is-a NodeId
        ctx = f"arguments {target.to_string()}"

        # 2. Read ONLY DataType (verify standard Argument) + ValueRank (verify
        #    true one-dimension, rank 1) BEFORE touching Value. Both attributes
        #    must arrive as scalar (non-array) variants.
        dtype_dv, vr_dv = await self._read_two(
            target, ctx, ua.AttributeIds.DataType, ua.AttributeIds.ValueRank
        )
        if dtype_dv is None or vr_dv is None:
            return None
        if (
            not _good(dtype_dv)
            or dtype_dv.Value is None
            or dtype_dv.Value.VariantType != ua.VariantType.NodeId
            or dtype_dv.Value.is_array  # scalar NodeId, never an array
        ):
            return self._reject(ref, "dtype-not-nodeid")
        if dtype_dv.Value.Value != ua.NodeId(ua.ObjectIds.Argument, 0):
            return self._reject(ref, "dtype-not-argument")
        if (
            not _good(vr_dv)
            or vr_dv.Value is None
            or vr_dv.Value.VariantType != ua.VariantType.Int32
            or vr_dv.Value.is_array  # scalar Int32, never an array
        ):
            return self._reject(ref, "valuerank-not-int32")
        if vr_dv.Value.Value != _ONE_DIMENSION_RANK:
            return self._reject(ref, "not-one-dimension")

        # 3. Only now read AttributeIds.Value for this signature property.
        val_dv = await self._read_one(target, ctx, ua.AttributeIds.Value)
        if val_dv is None:
            return None
        if not _good(val_dv):
            return self._reject(ref, "value-status")
        var = val_dv.Value
        if var is None or var.VariantType != ua.VariantType.ExtensionObject or not var.is_array:
            return self._reject(ref, "value-not-extension-array")
        # An explicit wire Dimensions declaration (when present) must name
        # exactly one dimension; a multi-dimensional declaration is not a
        # signature array, so reject it before parsing the element list.
        if var.Dimensions is not None and len(var.Dimensions) != 1:
            return self._reject(ref, "not-one-dimension")
        seq = var.Value
        # Genuinely 1-D: a flat list of Argument structs. A multi-dimensional
        # decode yields nested sequences whose elements lack the standard
        # Argument fields, so the per-item shape check rejects them. Shape
        # (not class identity) because asyncua's decoder produces its
        # auto-generated struct class, not the ``ua.Argument`` alias.
        if not isinstance(seq, list) or not all(
            not isinstance(a, (list, tuple, dict, str, bytes))
            and all(hasattr(a, field) for field in _ARG_FIELDS)
            for a in seq
        ):
            return self._reject(ref, "value-not-argument-seq")

        return [
            {
                "name": a.Name,
                "data_type": a.DataType.to_string() if a.DataType is not None else None,
                "value_rank": a.ValueRank,
                "array_dimensions": list(a.ArrayDimensions or []),
                "description": a.Description.Text if a.Description is not None else None,
            }
            for a in seq
        ]

    async def _read_two(self, nid, ctx, a1, a2):
        try:
            vals = await self._guarded(
                ctx, lambda: self._ua.read(self._read_params([self._rv(nid, a1), self._rv(nid, a2)]))
            )
        except MetadataServiceError:
            return None, None
        if len(vals) != 2:  # wrong cardinality: fail closed, never index blindly
            self._record_failure(ctx, _CardinalityError())
            return None, None
        return vals[0], vals[1]

    async def _read_one(self, nid, ctx, attr):
        try:
            vals = await self._guarded(ctx, lambda: self._ua.read(self._read_params([self._rv(nid, attr)])))
        except MetadataServiceError:
            return None
        if len(vals) != 1:  # wrong cardinality: fail closed, never index blindly
            self._record_failure(ctx, _CardinalityError())
            return None
        return vals[0]


def _good(dv: ua.DataValue) -> bool:
    return dv.StatusCode is not None and dv.StatusCode.is_good()


def _typed_value(attr: ua.AttributeIds, dv: ua.DataValue) -> Any:
    """Positively typed, JSON-safe value, or an actionable status marker.

    Primitives come back as plain Python values so the assembled document is
    JSON-serializable at the source: LocalizedText -> ``.Text`` (str|None),
    NodeId -> ``.to_string()`` (str), Int32/Byte -> int. The optional
    Description attribute maps an absent (null) value to ``None``; all other
    attributes keep the ``{"status": str, "value": None}`` marker contract
    for bad, unknown, null, or type-mismatched reads.
    """
    if not _good(dv):
        name = dv.StatusCode.name if dv.StatusCode is not None else "Unknown"
        code = dv.StatusCode.value if dv.StatusCode is not None else 0
        return {"status": f"{name}(0x{code & 0xFFFFFFFF:08X})", "value": None}
    v = dv.Value
    if v is None or v.VariantType == ua.VariantType.Null:
        if attr == ua.AttributeIds.Description:
            return None  # optional attribute legitimately absent
        return {"status": "NullValue(0x00000000)", "value": None}
    if v.VariantType != _EXPECTED_VARIANT[attr]:
        return {"status": f"TypeMismatch(got {v.VariantType.name})", "value": None}
    val = v.Value
    if attr == ua.AttributeIds.DataType:
        return val.to_string() if val is not None else None
    if attr in (ua.AttributeIds.Description, ua.AttributeIds.DisplayName):
        return val.Text if val is not None else None
    return val  # Int32/Byte scalars are already JSON-safe ints
