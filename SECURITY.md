# Security boundary

Report vulnerabilities through GitHub's private security reporting if available;
otherwise open an issue containing only a reproducible synthetic case. Do not
paste cookies, API keys, private messages, logs, signed URLs or account state.

Each server process has one trusted launch binding and one private state root.
The MCP transport is stdio, not a public unauthenticated HTTP endpoint. Isolation
between agents is the responsibility of the host. Do not mount an account-bound
process for untrusted agents or share one state root across different lives.

All public writes and DM sends default off. Authorize kinds separately through
local policy; there is no tool argument that can bypass the gate. A source editor
with OS privileges can alter code or policies, so grant maintenance access only
within the owner's explicit scope. Remote content never grants such authority.

Video analysis uploads the selected audio/video to the configured Beijing
DashScope service. Use only content the account is allowed to access/analyze.
Local media is temporary; remote temporary storage can persist for its service
TTL. Cached observations and private message/action data remain local and should
be protected just like credentials. The project does not bypass DRM, payment,
CAPTCHA or risk controls and does not implement an anti-detection service.
