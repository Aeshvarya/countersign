"""A13: a bearer token for Airwallex's merchant MCP, straight into your shell.

    set -a; . ./.env; set +a
    eval "$(python scripts/mcp_token.py)"

Airwallex's merchant MCP takes the normal Airwallex login token: POST
/api/v1/authentication/login with an API key that can read Payment Acceptance.
It lasts about 30 minutes. This prints one line, `export AIRWALLEX_MCP_TOKEN=...`,
for your shell to run; then `claude mcp add ... --header "Authorization: Bearer
$AIRWALLEX_MCP_TOKEN"` reads it from there (docs/RUNBOOK.md).

The token never goes to a file, a log or the screen. If stdout is a terminal
(you ran it without eval), it refuses rather than print it, because the screen
may be recording.

Exit 0: the export line is on stdout. 2: no token (keys missing, login refused,
or stdout is a terminal).
"""

from __future__ import annotations

import os
import shlex
import sys
import time
from typing import Mapping, TextIO

from countersign.airwallex import Airwallex, AirwallexError

VAR = "AIRWALLEX_MCP_TOKEN"
LIFETIME_MIN = 30  # Airwallex's login token, per its docs


def client_from(environ: Mapping[str, str]) -> Airwallex:
    """The merchant's own key. The PSP pair, if set, is the one Airwallex charges with, not this one."""
    missing = [k for k in ("AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY") if not environ.get(k)]
    if missing:
        raise RuntimeError(f"set {', '.join(missing)} in .env, then: set -a; . ./.env; set +a")
    return Airwallex(environ["AIRWALLEX_CLIENT_ID"], environ["AIRWALLEX_API_KEY"],
                     environ.get("AIRWALLEX_BASE_URL", "https://api.sandbox.airwallex.com"))


def main(environ: Mapping[str, str] = os.environ, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    if out.isatty():
        print(f'refusing to print the token to a terminal. Run: eval "$(python {sys.argv[0]})"', file=err)
        return 2
    try:
        token = client_from(environ).token()
    except RuntimeError as e:
        print(e, file=err)
        return 2
    except AirwallexError as e:  # the status and Airwallex's code only: the body never carries the key
        code = e.body.get("code") if isinstance(e.body, dict) else None
        print(f"Airwallex refused the login: {e.status}{f' {code}' if code else ''}. "
              "Check the key, and that it can read Payment Acceptance.", file=err)
        return 2
    print(f"export {VAR}={shlex.quote(token)}", file=out)
    until = time.strftime("%H:%M", time.localtime(time.time() + LIFETIME_MIN * 60))
    print(f"{VAR} set, valid until about {until}. Re-run this, then remove and re-add the MCP server, "
          "to refresh it.", file=err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
