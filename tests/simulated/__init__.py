"""SIMULATED / OFFLINE CleanAPIs testing layer.

Everything in this package is a **local simulation** of the external CleanAPIs
service for automated testing. It:

* runs entirely on ``127.0.0.1`` with an ephemeral port (real sockets, real
  HTTP — but never leaves the machine);
* requires **no real credentials** — it accepts one clearly-fake key;
* exercises the **production** ``CleanAPIsProvider`` / ``CleanAPIsClient``
  code path end to end (Application → ``LLMProvider`` interface →
  ``CleanAPIsProvider`` → simulated CleanAPIs HTTP endpoint → response
  parsing → normalized internal response);
* never contacts the real CleanAPIs API and never claims real connectivity.

Tests here are marked ``simulated`` and ``offline``. The REAL CleanAPIs
connectivity test lives in ``tests/integration/`` (marked ``live``) and is
reserved for a manual owner-run with a real key.
"""
