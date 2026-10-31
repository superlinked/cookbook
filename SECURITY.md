# Security Policy

## Reporting a vulnerability

Report vulnerabilities privately. Do not open a public issue, pull request or
discussion for them.

- Use GitHub private vulnerability reporting:
  [report a vulnerability](https://github.com/superlinked/cookbook/security/advisories/new).
- If you cannot use it, email support@superlinked.com with "SIE cookbook
  security report" in the subject. Leave the details out of that first message:
  we reply with a private channel for them.

Include the recipe folder, the commit, how to reproduce it, and the impact you
observed. For a vulnerability in SIE itself (the server, gateway, SDKs or Helm
chart), report it to [superlinked/sie](https://github.com/superlinked/sie/security)
instead.

## Secrets and data

Recipes read `SIE_API_KEY` from `.env`, which git ignores. If you find a real
credential, personal data or a live URL committed here, report it the same way.
All recipe data should be synthetic or from a public dataset that allows
redistribution.
