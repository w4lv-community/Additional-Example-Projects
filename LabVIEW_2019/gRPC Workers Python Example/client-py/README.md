# Workers Scope - Python client

The Python front end for the **Workers for LabVIEW** gRPC Scope Simulator
example. LabVIEW is the server and owns the instrument; this PySide6 scope GUI
is the client. `scope.proto` (one folder up) is the whole contract and
`scope_panel.py` is one self-contained file: read those two and you have the
entire example.

## Run

Once, in PowerShell, from this folder:

    pip install -r requirements.txt

Start the Workers gRPC Scope Simulator in LabVIEW, then start the client -
double-click `scope_panel.py`, or in PowerShell:

    py scope_panel.py

Connect -> Run. The server address is set in the panel's CONNECTION field -
point it at any machine running the Workers application.
