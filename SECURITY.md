# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Agent Forge, please report it responsibly.

**⚠️ Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email the maintainers or use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability).

### What to include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

### Response timeline

- **Acknowledgment:** Within 48 hours
- **Initial assessment:** Within 1 week
- **Fix or mitigation:** Depends on severity

## Security Considerations

Agent Forge executes code inside Docker containers. While the sandbox provides isolation, users should be aware of:

1. **Docker is not a security boundary** equivalent to a VM. For high-security use cases, consider the Firecracker microVM backend (planned for Phase 6).
2. **API keys** are stored in environment variables and are never passed into sandbox containers or written to logs.
3. **Network access** is disabled by default in sandboxes (`--network none`).
4. **Resource limits** (CPU, memory, PIDs) are enforced on all sandbox containers.

## Supported Versions

| Version | Supported |
| ------- | --------- |
| latest  | ✅        |
