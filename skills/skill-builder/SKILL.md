---
name: skill-builder
description: Build and improve Claude Code skills. Use when creating new skills, updating existing skills, debugging skill issues, or learning how to write effective SKILL.md files. Trigger words - skill, SKILL.md, new skill, create skill, write a skill, skill not working.
---

# Skill Builder

Guide for building effective skills for Claude Code. Based on Anthropic's official "Complete Guide to Building Skills for Claude" (Jan 2026).

**Reference:** [The Complete Guide to Building Skills for Claude](https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf)

## What is a Skill?

A skill is a folder containing instructions that teach Claude how to handle specific tasks or workflows. Skills are powerful for **repeatable workflows**: generating documents, conducting research, automating multi-step processes.

## Skill Structure

```
your-skill-name/
├── SKILL.md            # Required - main instructions
├── scripts/            # Optional - executable code
│   └── tool.py
├── references/         # Optional - docs loaded as needed
│   └── api-guide.md
└── assets/             # Optional - templates, fonts, icons
    └── template.md
```

## Critical Rules

### File Naming
- **SKILL.md must be exactly `SKILL.md`** (case-sensitive)
- No variations: `SKILL.MD` ❌, `skill.md` ❌, `Skill.md` ❌

### Folder Naming
- Use **kebab-case**: `notion-project-setup` ✅
- No spaces: `Notion Project Setup` ❌
- No underscores: `notion_project_setup` ❌
- No capitals: `NotionProjectSetup` ❌

### No README.md
- Don't include README.md inside skill folder
- All documentation goes in SKILL.md or `references/`

## YAML Frontmatter (Required)

The frontmatter is how Claude decides whether to load your skill. **Get this right.**

### Minimal Format

```yaml
---
name: your-skill-name
description: What it does and when to use it. Include trigger phrases.
---
```

### Field Requirements

**name** (required):
- kebab-case only
- Should match folder name

**description** (required):
- **MUST include BOTH:**
  - What the skill does
  - When to use it (trigger conditions)
- Under 1024 characters
- **No XML tags (< or >)** - security restriction
- Include specific phrases users might say
- Mention file types if relevant

**Optional fields:**
- `license`: MIT, Apache-2.0, etc.
- `compatibility`: Environment requirements (1-500 chars)
- `metadata`: Custom key-value pairs (author, version, mcp-server)

### Good Description Examples

```yaml
# Good - specific and actionable
description: Analyzes Figma design files and generates developer handoff documentation. Use when user uploads .fig files, asks for "design specs", "component documentation", or "design-to-code handoff".

# Good - includes trigger phrases
description: Manages Linear project workflows including sprint planning, task creation, and status tracking. Use when user mentions "sprint", "Linear tasks", "project planning", or asks to "create tickets".

# Good - clear value proposition
description: End-to-end customer onboarding workflow for PayFlow. Handles account creation, payment setup, and subscription management. Use when user says "onboard new customer", "set up subscription", or "create PayFlow account".
```

### Bad Description Examples

```yaml
# Too vague
description: Helps with projects.

# Missing triggers
description: Creates sophisticated multi-page documentation systems.

# Too technical, no user triggers
description: Implements the Project entity model with hierarchical relationships.
```

## Progressive Disclosure (3 Levels)

Skills use a three-level system to minimize token usage:

1. **YAML frontmatter** - Always loaded in Claude's system prompt. Just enough info to know when to use the skill.

2. **SKILL.md body** - Loaded when Claude thinks skill is relevant. Contains full instructions.

3. **Linked files** - Additional files in `references/` or `assets/` that Claude discovers as needed.

**Keep SKILL.md under 5,000 words.** Move detailed docs to `references/`.

## Writing Main Instructions

After the frontmatter, write instructions in Markdown:

```markdown
---
name: your-skill
description: [...]
---

# Your Skill Name

## Instructions

### Step 1: [First Major Step]
Clear explanation of what happens.

Example:
```bash
python scripts/fetch_data.py --project-id PROJECT_ID
```

Expected output: [describe what success looks like]

(Add more steps as needed)

## Examples

### Example 1: [common scenario]
User says: "Set up a new marketing campaign"
Actions:
1. Fetch existing campaigns via MCP
2. Create new campaign with provided parameters
Result: Campaign created with confirmation link

## Troubleshooting

### Error: [Common error message]
**Cause:** [Why it happens]
**Solution:** [How to fix]
```

## Best Practices

### Be Specific and Actionable

✅ **Good:**
```
Run `python scripts/validate.py --input {filename}` to check data format.
If validation fails, common issues include:
- Missing required fields (add them to the CSV)
- Invalid date formats (use YYYY-MM-DD)
```

❌ **Bad:**
```
Validate the data before proceeding.
```

### Include Error Handling

```markdown
## Common Issues

### MCP Connection Failed
If you see "Connection refused":
1. Verify MCP server is running: Check Settings > Extensions
2. Confirm API key is valid
3. Try reconnecting: Settings > Extensions > [Your Service] > Reconnect
```

### Reference Bundled Resources

```markdown
Before writing queries, consult `references/api-patterns.md` for:
- Rate limiting guidance
- Pagination patterns
- Error codes and handling
```

## Recording a Lesson (Self-Healing Edits)

When a task fails or a skill was misleading, fix the skill, but **edit it like documentation, not like a diary.** Skills are git-tracked and upstreamed to other installs, so every line has to be true, general, and free of personal data.

1. **Edit the relevant section in place.** Find where the wrong or missing guidance lives (the step, the troubleshooting entry, the flag table) and change it there. Never `cat >> SKILL.md` a new block at the bottom.
2. **Delete or fix whatever the lesson contradicts.** If the old text says "use X" and you learned "X breaks, use Y", rewrite the old text. Two sections that disagree are worse than none, because the next session can't tell which one wins.
3. **State the general rule, not the incident.** Write "Walmart checkout needs the card iframe filled via `fill-card-iframe`", not "On Tuesday the Walmart order failed because...". Keep a one-line *why* if it helps future judgment.
4. **No dates as headings.** No `## Lessons (YYYY-MM-DD)`, no `### Fix from the <date> call`. The rule should hold no matter when it was learned, and git history already records the when. ("as of" freshness notes on prices are fine.)
5. **Keep personal and one-off data out of skills.** That covers names, phone numbers, emails, home addresses, order or confirmation numbers, license plates, card digits, LAN IPs, chat ids, and `/Users/<name>/` paths. Where it goes instead:
   - Facts about a person (preferences, vehicle, address) go in **memory**, meaning Contacts notes or facts (see the `memory` / `contacts` skills).
   - What went wrong and why goes in **`~/dispatch/POSTMORTEMS.md`**.
   - When the skill must refer to a person or account, use a runtime lookup such as ``!`identity owner.name` ``, `identity owner.phone`, `identity assistant.email`, or "the admin" / "the owner". Use `~` for paths and placeholders like `192.168.x.x` and `you@example.com` in examples.
   - Live state (an active watch, a booked table) belongs in the skill's state file or reminders, not in SKILL.md.

**Bad** (appended at the bottom):
```markdown
## Lessons (YYYY-MM-DD, <owner>)
- <owner>'s order #XXXXXXX failed because the 10.0.x.x printer was asleep. Wake it first!
```

**Good** (edited into the existing Troubleshooting section):
```markdown
### Print job hangs
The printer sleeps and drops the first job. Wake it with `printer wake` and retry once.
```

### Lint before you finish

```bash
~/.claude/skills/skill-builder/scripts/skill-lint --changed      # lines you added/changed vs git HEAD
~/.claude/skills/skill-builder/scripts/skill-lint path/to/SKILL.md  # one file, every line
~/.claude/skills/skill-builder/scripts/skill-lint --summary       # whole repo + counts by rule
```

Output is `file:line:rule:snippet`. The rules are `dated-heading`, `private-ip`, `owner-name`/`partner-name` (resolved at runtime via `identity`), `abs-user-path`, `phone`, `email`, and `order-number`. The test suite runs `skill-lint --changed --strict`, so new hits fail tests. Hits that predate the linter are stored as hashes in `lint-baseline.txt` (the cleanup backlog). When you clean one up, its baseline line goes stale and is harmless; regenerate it with `skill-lint --write-baseline`. To keep a reviewed false positive, add `skill-lint: ignore` on that line (e.g. in an HTML comment).

## Common Skill Categories

### Category 1: Document & Asset Creation
Creating consistent, high-quality output (documents, designs, code).
- Embedded style guides and brand standards
- Template structures for consistent output
- Quality checklists before finalizing

### Category 2: Workflow Automation
Multi-step processes that benefit from consistent methodology.
- Step-by-step workflow with validation gates
- Templates for common structures
- Iterative refinement loops

### Category 3: MCP Enhancement
Workflow guidance to enhance MCP tool access.
- Coordinates multiple MCP calls in sequence
- Embeds domain expertise
- Error handling for common MCP issues

## Troubleshooting Skills

### Skill Won't Upload

**Error:** "Could not find SKILL.md in uploaded folder"
- Rename to `SKILL.md` (case-sensitive)
- Verify with: `ls -la | grep SKILL`

**Error:** "Invalid frontmatter"
- Check YAML has `---` delimiters at start and end
- Check quotes are closed properly
- Verify no XML angle brackets (`<` `>`)

**Error:** "Invalid skill name"
- Name must be kebab-case, no spaces/capitals

### Skill Doesn't Trigger

**Symptom:** Skill never loads automatically

**Fix:** Revise your description field.
- Is it too generic? ("Helps with projects" won't work)
- Does it include trigger phrases users would actually say?
- Does it mention relevant file types if applicable?

**Debug:** Ask Claude: "When would you use the [skill name] skill?" Claude will quote the description back.

### Skill Triggers Too Often

**Symptom:** Skill loads for unrelated queries

**Solutions:**
1. Add negative triggers: "Do NOT use for simple data exploration"
2. Be more specific about use cases
3. Clarify scope in description

### Instructions Not Followed

**Causes:**
1. Instructions too verbose - Keep concise, use bullet points
2. Instructions buried - Put critical stuff at top, use ## headers
3. Ambiguous language - Be explicit

**Advanced:** For critical validations, use scripts that check programmatically rather than language instructions. Code is deterministic; language interpretation isn't.

## Quick Checklist

**Before you start:**
- [ ] Identified 2-3 concrete use cases
- [ ] Planned folder structure

**During development:**
- [ ] Folder named in kebab-case
- [ ] SKILL.md exists (exact spelling)
- [ ] YAML has `---` delimiters
- [ ] name field: kebab-case, no spaces
- [ ] description includes WHAT and WHEN
- [ ] No XML tags anywhere
- [ ] Instructions are clear and actionable
- [ ] Error handling included
- [ ] Examples provided
- [ ] No personal data or dated diary headings (`skill-lint` is clean)

**Testing:**
- [ ] Triggers on obvious tasks
- [ ] Triggers on paraphrased requests
- [ ] Doesn't trigger on unrelated topics

## Creating a New Skill

```bash
# 1. Create folder
mkdir -p ~/.claude/skills/my-new-skill

# 2. Create SKILL.md with proper structure
cat > ~/.claude/skills/my-new-skill/SKILL.md << 'EOF'
---
name: my-new-skill
description: [What it does]. Use when [trigger phrases].
---

# My New Skill

## Instructions

[Your instructions here]

## Examples

[Examples here]

## Troubleshooting

[Common issues and fixes]
EOF

# 3. Optional: Add scripts directory
mkdir -p ~/.claude/skills/my-new-skill/scripts

# 4. Test it by asking Claude to use the skill
```

## Security Notes

**Forbidden in frontmatter:**
- XML angle brackets (`<` `>`)
- Skills with "claude" or "anthropic" in name (reserved)

**Why:** Frontmatter appears in Claude's system prompt. Malicious content could inject instructions.

## Resources

- [Skills Documentation](https://docs.anthropic.com/skills)
- [Example Skills Repo](https://github.com/anthropics/skills)
- [MCP Documentation](https://modelcontextprotocol.io)
