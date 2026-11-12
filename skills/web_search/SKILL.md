# Web search

This skill is not enabled. SSIOS does not ship a keyless search provider, and it does not call a cloud model to search.

A provider implements one method, `search(query) -> list[{title, url, snippet}]`, and returns only public `https` URLs. Those URLs still go through the same GET-only broker as `web_fetch`: no cookies, no request body, and the SSRF checks apply. Until that provider exists, ask for a specific URL instead.
