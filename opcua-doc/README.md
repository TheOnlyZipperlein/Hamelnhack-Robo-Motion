# OPC-UA-Lese-Tool – Dokumentation (`opcua-doc`)

Dieses Dokument beschreibt Installation, Aufruf und das Sicherheits-,
Grenzwert- und Abbruchverhalten des lesefähigen OPC-UA-Scan-Tools (`main.py`)
in diesem Ordner. Das Tool liest ausschließlich begrenztes Metadatenmaterial
aus einem OPC-UA-Server aus und schreibt die Ergebnisse als Markdown/JSON in
einen Ausgabeordner. Es sendet **keine** Steuer- oder Schreibanweisungen an
den Server. Die hier dokumentierte Schnittstelle ist der verbindliche
Vertragsentwurf; die CLI-Auswertung liegt in `config.py` in diesem Ordner,
die Verbindungslayer (`main.py`) werden separat ausgeliefert.

## 1. Voraussetzungen

- Python **>= 3.11**
- `asyncua==2.1.0` – siehe [`requirements.txt`](./requirements.txt)
- Erreichbarkeit des OPC-UA-Endpunkts (Standard: `opc.tcp://192.168.5.99:4840`)

## 2. Installation – nur manuell durch den Nutzer

> **Achtung:** Die nachfolgenden PowerShell-Befehle sind reine Anleitung.
> Sie wurden **nicht** automatisch ausgeführt und sind erst nach eigener
> Prüfung manuell auszuführen.

```powershell
# Aus dem Projektordner heraus:
python -m venv .venv                          # nur beim ersten Mal
.\.venv\Scripts\Activate.ps1                  # ggf. durch Execution Policy blockiert
python -m pip install --upgrade pip
python -m pip install -r opcua-doc\requirements.txt
```

Wird `Activate.ps1` durch die Windows Execution Policy blockiert, nicht
umgehen, sondern den Interpreter der Umgebung direkt aufrufen:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r opcua-doc\requirements.txt
```

## 3. Aufruf – nur manuell durch den Nutzer

> **Achtung:** Auch diese Befehle sind reine Anleitung und werden nicht
> automatisch ausgeführt.

Aus dem Ordner `opcua-doc/` heraus:

```powershell
# Minimalaufruf (nutzt alle Standardwerte)
python .\main.py
```

Vollständiges Beispiel mit abgesicherter Verbindung:

```powershell
python .\main.py `
  --endpoint opc.tcp://192.168.5.99:4840 `
  --security-policy Basic256Sha256 `
  --security-mode SignAndEncrypt `
  --username <benutzername> `
  --client-certificate .\certs\client.crt `
  --client-key .\certs\client.key `
  --server-certificate .\certs\server.crt `
  --application-uri urn:hamelnhack:robomotion:opcua-reader `
  --output-dir .\out
```

### 3.1 Parameter

| Parameter | Standard | Bedeutung |
| --- | --- | --- |
| `--endpoint` | `opc.tcp://192.168.5.99:4840` | OPC-UA-Endpunkt des Servers |
| `--security-policy` | `None` | Policy; Werte: `None`, `Basic256Sha256`, `Aes128Sha256RsaOaep`, `Aes256Sha256RsaPss` (¹) |
| `--security-mode` | `None` | `None`, `Sign` oder `SignAndEncrypt` |
| `--allow-insecure` | – | Explizite Freigabe für `--security-mode None` (Klartext); ohne das Flag wird Klartext abgelehnt |
| `--username` | – | Benutzername; nur zulässig mit `--security-mode SignAndEncrypt` |
| `--client-certificate` | – | X.509-Zertifikat der **Anwendung** (Client); Pflicht bei `Sign`/`SignAndEncrypt` |
| `--client-key` | – | Passender privater Schlüssel der Anwendung; Pflicht bei `Sign`/`SignAndEncrypt` |
| `--server-certificate` | – | Exakt erwartetes Serverzertifikat, ausbandig validiert und gepinnt; Pflicht bei `Sign`/`SignAndEncrypt`; **kein** Trust-on-First-Use |
| `--user-certificate` | – | X.509-**Benutzeridentität** (Account); exklusiv mit `--username` |
| `--user-key` | – | Privater Schlüssel zur Benutzeridentität; zusammen mit `--user-certificate` |
| `--application-uri` | nicht gesetzt | ApplicationURI des Clients (optional) |
| `--output-dir` | Arbeitsverzeichnis | Zielordner für die Markdown-/JSON-Berichte |
| `--root` | `Objects` (Standard-Ordner) | Startknoten des Scans als NodeId (z. B. `ns=4;s=\|appo\|c500.Application.GVL_Exchange`); der Scan startet an diesem Knoten statt im `Objects`-Ordner. Klassen, Browse- und Display-Name des Startknotens werden dabei mit einer zusätzlichen Metadaten-Lesung ermittelt; ein ungültiger oder nicht vorhandener Knoten beendet den Lauf mit Exit-Code `1` |

(¹) Die drei RSA-Policies stehen in `asyncua 2.1.0` zur Verfügung. Ob das Tool
alle drei in der finalen Umsetzung anbietet, ist mit der Implementierung zu
bestätigen; bis dahin gilt nur `Basic256Sha256` als sicher verfügbar.

Das Passwort wird **nicht** als Kommandozeilenparameter übergeben:

1. Umgebungsvariable `OPCUA_PASSWORD`, falls gesetzt;
2. andernfalls interaktive, verborgene Eingabe (Echo unterdrückt).

## 4. Grenzwerte (Standard)

| Grenze | Flag | Standard |
| --- | --- | --- |
| Browsentiefe | `--max-depth` | 2 Ebenen |
| Maximale Knotenzahl | `--max-nodes` | 50 |
| Maximale Anwendungsanfragen (Services) | `--max-requests` | 100 |
| Seitengröße (`BrowseNext`) | `--page-size` | 20 Einträge |
| Intervall zwischen Anfragen | `--request-interval` | 0,25 s |
| Zeitlimit pro Anfrage | `--timeout` | 5 s |
| Scan-Anfragen-Zeitlimit | `--scan-timeout` | 60 s |

Zusätzlich gilt:

- Der Anforderungsbudget (--max-requests) schließt eine Best-Effort-Fortsetzungsfreigabe ein; Aufbau- und Session-/Teardown-Anfragen sind davon getrennt.
- Das Scan-Zeitlimit (--scan-timeout) begrenzt die Zulassung und Timeouts neuer Scan-Anfragen; es ist keine harte Uhrgrenze zum Abbrechen oder Abbau der gesamten CLI.
- Eine einzige, sequenzielle Verbindung – keine parallelen Anfragen.
- Begrenztes Cleanup (z. B. Session-/Channel-Aufräumen nach Fehlern) ist
  vorgesehen und wird nicht allein als gescheiterter Scan gewertet.

## 5. Exit-Codes

| Code | Bedeutung |
| --- | --- |
| `0` | Keine protokollierten Lücken. Damit ist **keine** Vollständigkeit des Servers gewährleistet. |
| `1` | Fataler Fehler (z. B. keine Verbindung, ungültige Konfiguration). |
| `2` | Teilweise Ausführung – protokollierte Lücken (Limit erreicht, Timeouts, Sammelfehler). |

## 6. Abbildung auf UaExpert

| Feld im UaExpert-Dialog | Entsprechung in diesem Tool |
| --- | --- |
| Endpunkt-URL | `--endpoint` |
| „Security Policy“ (Dropdown: None, Basic…, Aes…) | `--security-policy` (hier nur RSA-Untermenge) |
| „Security Mode“ (None / Sign / Sign&Encrypt) | `--security-mode` |
| „User Authentication“: *Anonymous* | kein Token-Parameter (anonym) (²) |
| „User Authentication“: *User Name* (Felder Name + Passwort) | `--username` + `OPCUA_PASSWORD` bzw. verborgene Eingabe |
| „User Authentication“: *Certificate* (Zertifikatswahl) | `--user-certificate` + `--user-key` |
| Serverzertifikat: einmalig vertrauen / Trust Store | `--server-certificate` (exakt gepinnt, **kein** Trust-on-First-Use) |

(²) Anonym ist nur dann passend, wenn der Server anonyme Sitzungen mit
Leserecht zulässt und die gewählte Policy/Modus-Kombination es gestattet.

In UaExpert wählt man Policy, Modus und Authentifizierungsart frei aus und
verwaltet Serverzertifikate über einen Trust Store. Dieses Tool schränkt die
Policies auf die obige RSA-Untermenge ein und verlangt für jede abgesicherte
Kombination das exakte, ausbandig validierte Serverzertifikat.

## 7. Anwendungszertifikat vs. Benutzeridentität

- **Anwendungs-(Client-)Zertifikat** – identifiziert die *Anwendung* auf
  SecureChannel-Ebene; gebunden an die `ApplicationURI`; wird vom Server gegen
  seinen Trusted-Applications-Speicher geprüft; Angaben über
  `--client-certificate`/`--client-key`.
- **Benutzeridentität** – identifiziert den *Account* auf Sitzungsebene;
  entweder Username/Passwort (`--username`) oder X.509-Nutzerzertifikat
  (`--user-certificate`/`--user-key`); beide Varianten sind **exklusiv**.
- Beide Ebenen können kombiniert werden (z. B. App-Zertifikat für den Channel
  plus X.509-Benutzertoken für die Sitzung).
- **Anonym** – keine Benutzeridentität; nur geeignet, wenn der Server das
  erlaubt (siehe Fußnote (²)).

## 8. Umgang mit privaten Schlüsseln

- Private Schlüssel werden ausschließlich lokal aus den übergebenen Dateipaden
  gelesen; sie erscheinen nie in Ausgaben, Logs oder dem Versionsarchiv.
- Empfehlung: Die Zugriffsrechte der Schlüsseldateien auf das eigene Konto
  einschränken (NTFS-ACLs).
- `OPCUA_PASSWORD` enthält nur das Passwort – niemals Schlüsselmaterial.
- Schlüsseldateien nicht in Chat-, Ticket- oder Share-Systeme kopieren.

## 9. Hinweis: Betriebsmetadaten in den Ausgaben

Die erzeugten Markdown- und JSON-Dateien enthalten Betriebsmetadaten
(z. B. Endpunktadresse, Namespace-Namen, Knotennamen/-IDs, Zeitstempel) und
ermöglichen Rückschlüsse auf die Anlage. Sie sind daher als vertraulich zu
behandeln und nicht außerhalb des Teams weiterzugeben.

## 10. Erlaubte Operationen (Whitelist) – keine Steuerung

Das Tool ruft **nur** Folgendes ab:

- `Browse` und `BrowseNext` (seitig, max. 20 Einträge pro Seite);
- explizite Attributlesungen von Metadatenattributen (z. B. `BrowseName`,
  `DisplayName`, `DataType`, `Description`);
- bei `--root` zusätzlich eine Identifizierungslesung des Startknotens
  (`NodeClass`, `BrowseName`, `DisplayName`);
- die **einzig** zulässige `Value`-Leseoperation betrifft
  `InputArguments`/`OutputArguments` von Methode-Knoten: standardisierte,
  sauber typisierte Signaturenmeterdaten (`Argument[]`) – **keine**
  Prozessvariablen.

Explizit ausgeschlossen: Schreiberoperationen, Methodenaufrufe,
Subscriptions, Lesen von Prozess-Variablenwerten und jeglicher Fallback auf
andere Dienste. Scheitert eine erlaubte Leseoperation, wird eine Lücke
protokolliert (Exit-Code `2`), statt durch Alternativen ersetzt.
