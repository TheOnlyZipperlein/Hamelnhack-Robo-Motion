"""Graph-traversal core for the OPC UA address-space documentation tool.

Seam contract (wired by ``main.py``): ``scan(service, *, max_depth,
max_nodes, root=None)`` walks the local address space starting at the
standardized ``Objects`` folder -- or at a user-selected node when ``root``
is given -- and returns the structured documentation. All OPC UA transport
lives in the injected ``service``; this module performs no protocol I/O and
contains no robot-control functionality. It is read-only topology/metadata
assembly.

Injected service contract (implemented by ``services.MetadataService``):

* ``service.issues`` -- shared mutable ``list[str]``. The service appends
  its own per-node status/failure reports; :func:`scan` appends structural
  gap reports and returns the same list inside the result.
* ``async for ref in service.browse_children(node_id)`` -- async generator
  yielding ``ua.ReferenceDescription`` objects for the node's direct
  references. Paging/BrowseNext continuations are the service's concern;
  :func:`scan` stops consuming and explicitly ``aclose()``s the generator
  when a global limit is hit, so unused pages are not pulled.
* ``await service.identify(node_id)`` -- ``(node_class, browse_name,
  display_name)`` for a user-selected scan root; only consulted when
  ``root`` is not ``None``. Failing closed aborts the run.
* ``await service.metadata(node_id, node_class)`` -- ``dict`` with optional
  keys ``description``, ``display_name``, ``data_type``, ``value_rank``,
  ``access_level``, ``user_access_level``. Reasons for missing values are
  recorded by the service itself into ``service.issues``.
* ``await service.arguments(ref)`` -- ``list[dict]`` of decoded argument
  entries or ``None``. The service must confirm the target's exact standard
  ``PropertyType`` TypeDefinition and the ``Argument`` data type and decode
  only then. :func:`scan` never reads any ``Value`` attribute and never
  derives anything from values.

Argument admission gate applied here (fail closed, before the reference is
handed to the service): Method parent; direct, forward, namespace-0
``HasProperty`` reference; namespace-0 BrowseName ``InputArguments`` or
``OutputArguments``; target NodeClass ``Variable``; local target only. The
service makes the final confirmation; any rejection is reported through
``service.issues``.

Error policy: the service raises :class:`ScanLimit` only for globally
exhausted budgets (terminal: scanning stops, a partial result is returned
with ``complete=False``). Any other exception from a service call is a
per-node failure: it is recorded in ``service.issues`` (exception type name
only, no transport details) and scanning continues with ``None`` for the
affected fields. Per-node failures also include a metadata status marker
(``{"status": str, "value": None}`` from the service) on any read attribute;
the sole exemption is an explicit absence (``BadAttributeIdInvalid``) of the
optional ``description`` attribute -- for the required ``display_name``,
``data_type``, ``value_rank``, and access-level attributes that status is a
failure. A ``None`` returned by ``service.arguments`` for an admitted
signature property is likewise a failure (a method lacking the property
entirely is normal, not a failure). Every recorded per-node failure is
counted and forces ``complete=False`` in the returned document.

Traversal semantics: breadth-first from the root (the standardized Objects
folder, or a user-supplied node via ``root``); a node's canonical parent
is the first (BFS) edge that discovers it; all observed local edges are kept
(including multiple incoming edges and cycles); non-local (remote)
``ExpandedNodeId`` targets are skipped, counted, and reported, and are
never graphed, enqueued, or handed to argument handling; nodes at
``max_depth`` are recorded but not expanded; the node cap
stops both discovery and further page consumption.

Result schema::

    {
        "root": str,            # root NodeId, e.g. "i=85"
        "nodes": [node, ...],
        "edges": [{"parent_id": str, "child_id": str,
                   "reference_type": str}, ...],
        "issues": list[str],    # shared service.issues
        "complete": bool,
        "limits": {"max_depth": int, "max_nodes": int},  # CLI merges more
    }

Node keys: ``node_id, browse_name, display_name, description, node_class,
depth, parent_id, data_type, value_rank, access_level, user_access_level,
input_arguments, output_arguments``.
"""

from __future__ import annotations

from collections import deque
from typing import AsyncIterator, Protocol

from asyncua import ua

__all__ = ["ScanLimit", "ScanService", "scan"]


class ScanLimit(Exception):
    """Global scan budget (depth/node/request/time) exhausted.

    Raised by the service only; terminal for :func:`scan`, which returns a
    partial structured document with ``complete=False``.
    """


class ScanService(Protocol):
    """Transport-backed service interface consumed by :func:`scan`."""

    issues: list[str]

    def browse_children(
        self, node_id: ua.NodeId
    ) -> AsyncIterator[ua.ReferenceDescription]:
        """Yield the node's direct ReferenceDescriptions (paged internally)."""
        ...

    async def identify(self, node_id: ua.NodeId) -> tuple[ua.NodeClass, str, str | None]:
        """Return ``(node_class, browse_name, display_name)`` for a custom
        scan root; fail closed on any unusable node."""
        ...

    async def metadata(self, node_id: ua.NodeId, node_class: ua.NodeClass) -> dict:
        """Return the metadata dict; unreadable attributes arrive as
        ``{"status": str, "value": None}`` markers; record reasons into
        ``issues``."""
        ...

    async def arguments(self, ref: ua.ReferenceDescription) -> list[dict] | None:
        """Return decoded argument entries, or None; confirm types first."""
        ...


#: Standardized root of the address space (OPC UA Objects folder, ns0).
_ROOT_ID = ua.NodeId(ua.ObjectIds.ObjectsFolder)
#: Standardized BrowseName/DisplayName of the Objects folder (ns0).
_ROOT_NAME = "Objects"
#: Metadata keys copied from the service dict into the node record.
_META_KEYS = (
    "description",
    "data_type",
    "value_rank",
    "access_level",
    "user_access_level",
)
#: Service status marker declaring the attribute explicitly absent on the
#: node (server states it inapplicable). Legitimate only for the optional
#: ``description`` attribute; for required attributes it is a failure.
_EXEMPT_ABSENT_STATUS = "BadAttributeIdInvalid"


def _marker(value):
    """Split a service metadata value into ``(value, status)``.

    The service encodes bad, unknown, or type-mismatched reads as
    ``{"status": str, "value": None}``; positively typed values pass through
    untouched with ``status=None``.
    """
    if isinstance(value, dict) and "status" in value:
        return value.get("value"), value.get("status")
    return value, None


def _nid_str(node_id: ua.NodeId | None) -> str:
    """Stable string form of a NodeId (e.g. ``i=85``)."""
    return node_id.to_string() if node_id is not None else "<none>"


def _is_local(ref: ua.ReferenceDescription) -> bool:
    """True when the target ExpandedNodeId refers to this server only.

    Fail closed: a present, non-empty ``NamespaceUri`` marks the target as
    remote without ever reading the server's NamespaceArray Value; a
    missing/None/empty URI with an absent/None/0 ``ServerIndex`` is local,
    and any other combination is treated as remote.
    """
    node_id = ref.NodeId
    if node_id is None:
        return False
    if getattr(node_id, "NamespaceUri", None):
        return False
    server_index = getattr(node_id, "ServerIndex", 0)
    if server_index is None:
        server_index = 0
    return server_index == 0


def _is_direct_has_property(ref: ua.ReferenceDescription) -> bool:
    """Fail-closed gate: direct, forward, ns0, exact HasProperty reference.

    Subtypes of HasProperty are not resolvable from a bare
    ReferenceDescription; they are conservatively rejected here and the
    service performs the final type confirmation.
    """
    ref_type = ref.ReferenceTypeId
    return (
        ref.IsForward
        and ref_type.NamespaceIndex == 0
        and ref_type.Identifier == ua.ObjectIds.HasProperty
    )


def _argument_field(ref: ua.ReferenceDescription) -> str | None:
    """Map a candidate reference to its node-record field, or None."""
    name = ref.BrowseName
    if name is None or name.NamespaceIndex != 0:
        return None
    if name.Name == "InputArguments":
        return "input_arguments"
    if name.Name == "OutputArguments":
        return "output_arguments"
    return None


def _new_record(node_id: str, browse_name: str, node_class: ua.NodeClass,
                depth: int, parent_id: str | None) -> dict:
    return {
        "node_id": node_id,
        "browse_name": browse_name,
        "display_name": None,
        "description": None,
        "node_class": node_class.name,
        "depth": depth,
        "parent_id": parent_id,
        "data_type": None,
        "value_rank": None,
        "access_level": None,
        "user_access_level": None,
        "input_arguments": None,
        "output_arguments": None,
    }


async def scan(service, *, max_depth: int, max_nodes: int,
               root: ua.NodeId | None = None) -> dict:
    """Traverse the local address space and assemble the documentation.

    See the module docstring for the service contract, traversal semantics,
    and result schema. Raises ``ValueError`` for invalid limit arguments.
    When ``root`` is given (a ``ua.NodeId``), traversal starts at that node
    instead of the standardized Objects folder; the node is identified with
    one extra metadata read and an unusable root propagates as a hard error.
    """
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        raise ValueError("max_depth must be an int >= 0")
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or max_nodes < 1:
        raise ValueError("max_nodes must be an int >= 1")
    if root is not None and not isinstance(root, ua.NodeId):
        raise ValueError("root must be a ua.NodeId or None")

    issues: list[str] = service.issues
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    edge_keys: set[tuple[str, str, str]] = set()
    queue: deque[tuple[ua.NodeId, int, dict, ua.NodeClass]] = deque()
    limited = False
    cap_hit = False
    depth_cut_count = 0
    remote_skip_count = 0
    failures = 0

    def add_edge(parent_id: str, ref: ua.ReferenceDescription,
                 local: bool) -> None:
        nonlocal remote_skip_count
        if not local:
            remote_skip_count += 1
            issues.append(
                f"[scan] skip remote target {_nid_str(ref.NodeId)} "
                f"(parent {parent_id})"
            )
            return
        key = (parent_id, _nid_str(ref.NodeId), _nid_str(ref.ReferenceTypeId))
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append({
            "parent_id": parent_id,
            "child_id": _nid_str(ref.NodeId),
            "reference_type": _nid_str(ref.ReferenceTypeId),
        })

    def discover(ref: ua.ReferenceDescription, depth: int, parent_id: str) -> None:
        record = _new_record(
            _nid_str(ref.NodeId),
            ref.BrowseName.Name if ref.BrowseName is not None else "",
            ref.NodeClass,
            depth,
            parent_id,
        )
        nodes.append(record)
        seen.add(record["node_id"])
        queue.append((ref.NodeId, depth, record, ref.NodeClass))

    def absorb_meta(record: dict, meta: dict) -> None:
        """Copy service metadata into ``record``, unwrapping status markers.

        Only the optional ``description`` may be legitimately absent
        (``BadAttributeIdInvalid``); that status on any other attribute is
        a tracked per-node failure. An empty ``display_name`` keeps the
        prior fallback value.
        """
        nonlocal failures
        for key in ("display_name", *_META_KEYS):
            value, status = _marker(meta.get(key))
            if key == "display_name":
                if value:
                    record[key] = value  # empty/None keeps the prior fallback
            elif value is not None:
                record[key] = value
            if status is not None:
                # Services emit the status as ``name(hex)``; the exemption
                # must match the status name alone (before the hex suffix),
                # and only for the optional ``description`` attribute. The
                # full marker (with hex) is preserved in the issue.
                status_name = status.partition("(")[0]
                exempt = (
                    key == "description" and status_name == _EXEMPT_ABSENT_STATUS
                )
                if not exempt:
                    failures += 1
                    issues.append(
                        f"[scan] {key} unreadable for {record['node_id']}: {status}"
                    )

    # Root: the standardized Objects folder, or a user-selected node. A
    # custom root is identified (class, browse name, display name) with one
    # extra metadata read; an unusable root propagates as a hard error.
    if root is None:
        root_id = _ROOT_ID
        root_name = _ROOT_NAME
        root_class = ua.NodeClass.Object
        root_display: str | None = _ROOT_NAME
    else:
        root_id = root
        root_class, root_name, root_display = await service.identify(root_id)
    root_meta: dict = {}
    try:
        root_meta = await service.metadata(root_id, root_class)
    except ScanLimit:
        limited = True
    except Exception as exc:  # per-node failure: keep the known root
        failures += 1
        issues.append(f"[scan] root metadata failed: {type(exc).__name__}")
    root_record = _new_record(_nid_str(root_id), root_name, root_class, 0, None)
    root_record["display_name"] = root_display or root_name
    absorb_meta(root_record, root_meta)
    nodes.append(root_record)
    seen.add(root_record["node_id"])
    queue.append((root_id, 0, root_record, root_class))

    while queue and not limited:
        node_id, depth, record, node_class = queue.popleft()

        if len(nodes) >= max_nodes:
            cap_hit = True
            break  # do not spend further calls once the cap is reached

        if record is not root_record:
            try:
                meta = await service.metadata(node_id, node_class)
            except ScanLimit:
                limited = True
                break
            except Exception as exc:
                failures += 1
                issues.append(
                    f"[scan] metadata failed for {record['node_id']}: "
                    f"{type(exc).__name__}"
                )
                meta = {}
            absorb_meta(record, meta)

        if depth >= max_depth:
            depth_cut_count += 1
            continue

        arg_refs: dict[str, ua.ReferenceDescription] = {}
        gen = service.browse_children(node_id)
        try:
            async for ref in gen:
                # One locality verdict per reference, guarding graphing,
                # enqueueing, and argument handling alike.
                local = _is_local(ref)
                add_edge(record["node_id"], ref, local)
                if (
                    node_class == ua.NodeClass.Method
                    and local
                    and ref.NodeClass == ua.NodeClass.Variable
                    and _is_direct_has_property(ref)
                ):
                    field = _argument_field(ref)
                    if field is not None and field not in arg_refs:
                        arg_refs[field] = ref
                if len(nodes) >= max_nodes:
                    cap_hit = True
                    break
                if local and _nid_str(ref.NodeId) not in seen:
                    discover(ref, depth + 1, record["node_id"])
        except ScanLimit:
            limited = True
        except Exception as exc:
            failures += 1
            issues.append(
                f"[scan] browse failed for {record['node_id']}: "
                f"{type(exc).__name__}"
            )
        finally:
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                await aclose()  # release unused pages/continuations early

        if limited or cap_hit:
            break

        if node_class == ua.NodeClass.Method:
            for field, arg_ref in arg_refs.items():
                try:
                    record[field] = await service.arguments(arg_ref)
                except ScanLimit:
                    limited = True
                    break
                except Exception as exc:
                    failures += 1
                    issues.append(
                        f"[scan] arguments failed ({field} of "
                        f"{record['node_id']}): {type(exc).__name__}"
                    )
                    record[field] = None
                else:
                    # Admitted property: None means the service confirmed
                    # nothing decodable; an empty list is a valid signature.
                    if record[field] is None:
                        failures += 1
                        issues.append(
                            f"[scan] arguments unavailable ({field} of "
                            f"{record['node_id']})"
                        )
            if limited:
                break

    complete = not limited
    if limited:
        issues.append(
            "[scan] ScanLimit: global budget exhausted; "
            "partial documentation returned"
        )
    if cap_hit:
        complete = False
        issues.append(
            f"[scan] node cap {max_nodes} reached: "
            f"{len(queue)} queued nodes not scanned"
        )
    if depth_cut_count:
        complete = False
        issues.append(
            f"[scan] depth limit {max_depth} reached: "
            f"{depth_cut_count} nodes at max depth not expanded"
        )
    if failures:
        complete = False
        issues.append(
            f"[scan] {failures} per-node failure(s) recorded; "
            f"documentation is incomplete"
        )
    if remote_skip_count:
        issues.append(
            f"[scan] {remote_skip_count} remote (non-local) references "
            f"skipped, not graphed"
        )

    return {
        "root": _nid_str(root_id),
        "nodes": nodes,
        "edges": edges,
        "issues": issues,
        "complete": complete,
        "limits": {"max_depth": max_depth, "max_nodes": max_nodes},
    }
