# Security Policy

## Supported Versions
We actively provide security updates for the following versions of CLU:

| Version | Supported          |
| ------- | ------------------ |
| 4.10.x  | :white_check_mark: |
| 4.x     | :white_check_mark: |
| < 4.0   | :x:                |

## Reporting a Vulnerability
If you discover a security vulnerability within CLU, please **do not** open a public issue. Instead, use one of the following methods:

1. **GitHub Private Vulnerability Reporting:** Navigate to the "Security" tab of this repository and select "Report a vulnerability." This allows us to discuss the fix in private.
2. **Email:** [phillip.duncan@gmail.com]

Please include:
- A description of the vulnerability.
- Steps to reproduce (POC).
- Potential impact (e.g., unauthorized file access, API key exposure).

We aim to acknowledge all reports within 48 hours and provide a fix or mitigation strategy as quickly as possible.

## Security Focus Areas
Given the nature of CLU as a self-hosted management tool, we are particularly interested in reports concerning:
- **Authentication Bypass:** Issues regarding API headers or unauthorized access to the web UI.
- **File System Security:** Vulnerabilities that could allow access to files outside of the mapped `/data` or `/config` directories.
- **Credential Handling:** Insecure storage or exposure of third-party API keys (Metron, ComicVine, Gemini, etc.).
- **Remote Code Execution (RCE):** Vulnerabilities related to the processing of comic archives (CBZ/CBR) or metadata scraping.
