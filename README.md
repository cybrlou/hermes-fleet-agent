# Hermes fleet sidecar

Phone-home agent for Cybrtch Hermes boxes. **No hub IP or token in this repo.** The installer asks for them.

Installs **outside** the Hermes directory so `hermes update` does not stop the sidecar.

## Install

Windows (PowerShell):

```
git clone https://github.com/cybrlou/hermes-fleet-agent.git
cd hermes-fleet-agent
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Linux:

```
git clone https://github.com/cybrlou/hermes-fleet-agent.git
cd hermes-fleet-agent
sh ./install.sh
```

You will be asked for:

1. Hub address (IP or URL)
2. Agent id
3. Site name
4. Fleet token (not printed)

## Notes

- Sidecar path: `%LOCALAPPDATA%\hermes-fleet-agent` or `~/.local/share/hermes-fleet-agent`
- Task / service restarts on failure and at logon
- Chat finds `hermes` on PATH or the usual install location after an update
- If the hub health URL fails from that PC, its public IP is not on the firewall allow list
