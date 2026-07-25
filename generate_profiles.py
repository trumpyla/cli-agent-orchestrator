import os
import glob
import yaml

models = [
    {"provider": "codex", "model": "gpt-5.6-sol", "prefix": "codex-sol"},
    {"provider": "claude_code", "model": "opus", "prefix": "claude-opus"},
    {"provider": "antigravity_cli", "model": "Gemini 3.1 Pro (High)", "prefix": "agy-pro-high"},
    {"provider": "kimi_cli", "model": "kimi-code/k3", "prefix": "kimi-k3"}
]

roles = ["supervisor", "implementer", "tester", "researcher", "adversarial", "shell", "designer"]

base_skills = {
    "supervisor": ["cao-supervisor-protocols"],
    "designer": ["cao-worker-protocols", "python-design-patterns", "python-anti-patterns"],
    "implementer": ["cao-worker-protocols", "python-type-safety", "python-design-patterns", "python-error-handling", "python-resource-management", "async-python-patterns", "python-code-style", "python-anti-patterns", "python-testing-patterns"],
    "tester": ["cao-worker-protocols", "python-testing-patterns", "python-anti-patterns"],
    "researcher": ["cao-worker-protocols"],
    "adversarial": ["cao-worker-protocols", "python-anti-patterns", "python-testing-patterns"],
    "shell": ["cao-worker-protocols"]
}

role_map = {
    "supervisor": "supervisor",
    "designer": "reviewer",
    "implementer": "developer",
    "tester": "reviewer",
    "researcher": "reviewer",
    "adversarial": "reviewer",
    "shell": "developer"
}

mcp_servers = {
  "cao-mcp-server": {
    "type": "stdio",
    "command": "cao-mcp-server",
    "args": []
  },
  "context7": {
    "type": "http",
    "url": "${CAO_CONTEXT7_MCP_URL}"
  },
  "tavily": {
    "type": "http",
    "url": "${CAO_TAVILY_MCP_URL}"
  },
  "gemini-search": {
    "type": "http",
    "url": "${CAO_GEMINI_SEARCH_MCP_URL}"
  },
  "duckduckgo": {
    "type": "http",
    "url": "${CAO_DUCKDUCKGO_MCP_URL}"
  },
  "serena": {
    "type": "http",
    "url": "${CAO_SERENA_MCP_URL}"
  }
}

prompt_template = """
# SWARM {role_upper}

You are the {r} lane of this repository's development swarm, running on {model}.

## Operating Guidance & Lifecycle

- Follow your assigned `cao-worker-protocols` or `cao-supervisor-protocols` for inbox pickup, progress reporting, and handback.
- Work strictly inside your assigned scope/worktree. Never touch the main checkout or other lanes' files.
- Apply your lane-scoped skills: {skills_list}.
- The `review-verification-protocol` and `artagon-python-review`/`review-python` skills are stale and explicitly excluded.

## Managed MCP Surfaces & Navigation Protocol

Use each managed MCP server for its intended purpose:
- **context7** — Consult for current library/framework/API/CLI documentation BEFORE writing code against a third-party API.
- **tavily** and **gemini-search** — Source-backed current web research. Use BOTH for any current or version-sensitive claim.
- **duckduckgo** — Fallback/general web search when managed lanes are unavailable.
- **serena** — Read-only symbol navigation for this repository. Use for definitions, references, and usages BEFORE any broad text search.
- **cao-mcp-server** — CAO orchestration tools (inbox, handoff/assign, skill loading).

Structural navigation rule: use Serena symbol navigation and `sg` ast-grep structural queries before broad text search.
"""

os.makedirs(".cao/agents", exist_ok=True)

# Delete existing generic ones
for f in glob.glob(".cao/agents/*.md"):
    os.remove(f)

for m in models:
    for r in roles:
        name = f"{m['prefix']}-{r}"
        
        frontmatter = {
            "name": name,
            "description": f"{m['model']} profile for {r} role",
            "provider": m['provider'],
            "model": m['model'],
            "role": role_map[r],
            "skills": base_skills[r],
            "mcpServers": mcp_servers
        }
        
        if m['provider'] in ['claude_code', 'antigravity_cli']:
            if r in ['implementer', 'shell']:
                frontmatter['permissionMode'] = 'acceptEdits'
            else:
                frontmatter['permissionMode'] = 'plan'
        
        md_content = f"---\n{yaml.dump(frontmatter, sort_keys=False)}---\n"
        md_content += prompt_template.format(
            role_upper=r.upper(),
            r=r,
            model=m['model'],
            skills_list=", ".join(base_skills[r])
        )
        
        with open(f".cao/agents/{name}.md", "w") as f:
            f.write(md_content)

print("Generated precise swarm agents.")
