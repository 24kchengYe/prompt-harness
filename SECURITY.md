# Security policy

Please report security issues privately through GitHub's security-advisory workflow rather than opening a public issue with real prompt data, credentials, or transcripts.

Prompt Harness is local-first and performs no network upload. Publishing a repository that contains `.prompt-harness` data is outside the intended default. Before a release, run the unit tests, plugin validators, `prompt_harness.py doctor`, and a repository-wide secret scan.
