# Security

Please report security problems privately to **ashish@binggio.com** with "Security" in the subject, not in a
public issue. Include the Binggio version (`binggio --version`), your operating system, and how to
reproduce the problem. You will get an acknowledgement within a few working days.

What Binggio is designed to guarantee, and what counts as a security problem:

- `binggio ui` listens on this computer only (127.0.0.1) and refuses requests without its session
  token or from another host name.
- Database passwords and model keys entered in the app are kept in memory, never written to disk.
- Sources are read only; Binggio never writes to them.
- Answers use approved meanings only; no free SQL is accepted, from people, the model or MCP agents.
- MCP agents cannot approve, reject or revoke anything.
