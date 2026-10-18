Install the Cybrtch Hermes fleet sidecar on this machine so it phones home.

Clone https://github.com/cybrlou/hermes-fleet-agent.git (or pull if already cloned). Run install.ps1 on Windows or install.sh on Linux.

Ask the operator for four things. Do not guess. Do not print the token in chat.

1. Hub address (IP or URL)
2. Agent id (short name for this box)
3. Site name
4. Fleet token

The installer writes those into a local env file only. The GitHub repo has no IP and no token.

Install outside the Hermes folder. Start the sidecar now and at logon. Keep it running if Hermes is updated — do not uninstall it during an update. After update, the sidecar should still find hermes on PATH or under %LOCALAPPDATA%\hermes\hermes-agent (Windows) / the usual venv.

Prove: sidecar log shows connected. If hub health fails from this PC, report the public IP and stop. Do not open firewalls yourself.
