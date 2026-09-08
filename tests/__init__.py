"""Test package.

``app.main`` loads the developer's local ``.env`` at import, so without this the
suite inherits whatever credentials that file holds: it then makes real Gemini
and Nokia calls, bills them, and turns provider latency into flaky, minutes-long
runs.  CI never saw it because a checkout has no ``.env``.

Pinning the provider switches here makes the default posture deterministic and
offline.  A test that wants a provider configured still patches it explicitly,
which is also the only place such a dependency should be visible.
"""

import os

os.environ["GEMINI_API_KEY"] = ""
os.environ["NAC_LIVE_ENABLED"] = "false"
os.environ["NAC_SIMULATOR_ALLOW_UNSIGNED_CALLBACKS"] = "false"
os.environ["RESCUEROUTE_API_TOKEN"] = ""
os.environ["RESCUEROUTE_ACCESS_CODE"] = ""
