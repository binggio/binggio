# Releasing Binggio

## One-time setup

1. **PyPI Trusted Publishing** (no token is ever stored). Sign in at pypi.org, go to
   *Your account > Publishing > Add a new pending publisher* and enter:
   - PyPI project name: `binggio`
   - Owner: `binggio`, Repository: `binggio`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. **GitHub environment.** In the repository: *Settings > Environments > New environment*, name it
   `pypi`. Optionally add yourself under *Required reviewers* so every release waits for your click.
3. **MCP Registry, under the name `com.binggio/binggio`.** The registry checks that you own
   binggio.com through a DNS record. Do this once, in **Git Bash** (it includes `openssl`), in a
   folder outside the repository:
   ```
   openssl genpkey -algorithm Ed25519 -out binggio-mcp-key.pem
   openssl pkey -in binggio-mcp-key.pem -pubout -outform DER | tail -c 32 | base64
   ```
   - At your domain provider, add a **TXT** record on `binggio.com` (host `@`) with the value
     `v=MCPv1; k=ed25519; p=` followed by the line printed above. It can take up to an hour to appear.
   - Print the private key as hex, and paste it straight into GitHub as a secret (*Settings >
     Environments > pypi > Add environment secret*, name `MCP_DNS_PRIVATE_KEY`):
     ```
     openssl pkey -in binggio-mcp-key.pem -noout -text | grep -A3 "priv:" | tail -n +2 | tr -d ' :\n'
     ```
   - Keep `binggio-mcp-key.pem` somewhere safe and private (a password manager), never in the
     repository. Anyone with it can publish under binggio.com names in the registry. If it leaks,
     make a new pair and replace the TXT record and the secret.
   - The README line `<!-- mcp-name: com.binggio/binggio -->` proves the PyPI package belongs to
     this server.

## Each release

1. Set the same version in `pyproject.toml`, `src/binggio/__init__.py` and `server.json` (twice),
   then check: `python tools/check_version.py`
2. Make sure the **tests** workflow is green on `main`.
3. Tag and push:
   ```
   git tag v0.1.0
   git push origin v0.1.0
   ```
4. The **release** workflow then: checks the versions, builds the package and the Claude Desktop
   bundle, publishes to PyPI, publishes to the MCP Registry, and creates the GitHub release with
   the `.whl`, `.tar.gz` and `.mcpb` attached.

A version number can be published to PyPI only once. If something goes wrong after PyPI accepted
it, fix it and release the next number (for example 0.1.1).

## Trying the bundle before a release

```
python tools/build_mcpb.py --out build/mcpb
npx -y @anthropic-ai/mcpb validate build/mcpb/manifest.json
npx -y @anthropic-ai/mcpb pack build/mcpb binggio.mcpb
```
Open `binggio.mcpb` with Claude Desktop, choose a workspace folder, and ask a question.
