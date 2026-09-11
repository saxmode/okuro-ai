# Security & Privacy

Okuro is a single-user, local-first tool. It stores genuinely sensitive data — your cognitive profile, personal thoughts, work-in-progress notes, the contents of every conversation you have with the agents that talk to it. This document is honest about what okuro protects, what it doesn't, and what's on you.

## TL;DR

- Okuro relies on **operating-system file permissions** and your **full-disk encryption**. It does not encrypt the database itself.
- Anything you put into okuro that an AI agent reads will travel to whatever **AI provider** that agent is connected to (Anthropic, OpenAI, Google, etc.). Okuro does not redact, sanitize, or filter what gets sent.
- The hardening checklist below is **your responsibility**. Okuro will not enforce it.

## Threat model

What each protection actually buys you:

| Threat | OS file perms (`700`/`600`) | Full-disk encryption | Provider no-train settings |
|--------|------------------------------|----------------------|----------------------------|
| Other non-root users on the same machine | Yes | Yes | N/A |
| Stolen device, disk not encrypted | No | Yes | N/A |
| Cloud backup / rsync of `~/.okuro/` to an unencrypted destination | No | No | N/A |
| Accidental `git commit` of `~/.okuro/` | No | No | N/A |
| Malware running as your user | No | No | N/A |
| Root on the machine | No | No | N/A |
| Memory dump while okuro is running | No | No | N/A |
| AI provider trains on the prompts your agent sends them | N/A | N/A | Yes |

If a row says "No" everywhere, it means okuro cannot protect against that threat — see the responsibilities checklist.

## User responsibilities

Things okuro does not do for you, in priority order:

- **Enable full-disk encryption.** macOS FileVault, Linux LUKS, equivalent on your distro. Without this, anyone who steals the machine reads everything.
- **Do not commit `~/.okuro/` to git.** If your home directory or any ancestor of `~/.okuro/` is a git working tree, add `.okuro/` to that repo's `.gitignore`. Okuro warns you on bootstrap and in `okuro doctor` if it detects this risk, but cannot prevent it.
- **Do not sync `~/.okuro/` to cloud storage that you don't trust.** Dropbox, iCloud Drive, Google Drive, OneDrive, raw `rsync` to a NAS — all of these turn the database file into a portable copy. Either exclude `~/.okuro/` from sync, or use end-to-end encrypted sync.
- **Configure your AI providers' "do not train" settings.** Each provider has its own switch:
  - Anthropic / Claude: enterprise plans do not train on inputs by default; consumer Claude has settings under your account.
  - OpenAI: per-account "Improve the model for everyone" toggle, plus separate API-tier opt-outs.
  - Google / Gemini: distinct settings for free vs paid tiers.
  - Verify the policy that applies to **the specific account and tier** the CLI is signed in to.
- **Do not run okuro as a user that other people can `sudo` into.** Same machine, different users is fine — same machine, shared sudo is not.

## AI provider data sharing

This is the largest privacy surface and the one most people underestimate.

Okuro is wiring infrastructure. It hands assembled context (your profile, memories, project state, etc.) to whatever agent calls `bootstrap()`. Once the agent has it, every subsequent token the agent generates may be informed by that context, and every prompt the agent sends to its provider includes whatever portion of that context the agent decides is relevant.

Concretely:

- Your **cognitive profile** — neurotype, communication preferences, anything you put in onboarding — gets included in bootstrap packets and is therefore visible to the provider.
- Your **memories, thoughts, and progress logs** can be searched by the agent and appear in prompts.
- Your **project context, principles, and stack info** is in the bootstrap packet by default.
- **Conversations you have with the agent** are processed by the provider regardless of okuro.

Okuro does not strip personal identifiers, redact sensitive fields, or warn the agent about sensitivity tiers. If you write something into a memory that you do not want a third party to ever read, do not write it into a memory.

Mitigations:

- Audit your provider account settings and confirm no-train is on for the tier you actually use.
- Self-host an open-weight model and connect okuro to a local CLI if your data sensitivity demands zero third-party exposure.
- Treat the cognitive profile and memory contents like email: assume they are stored on someone else's computer.

## What okuro does protect

Honest, limited list:

- **OS file permissions.** `~/.okuro/` and the SQLite database are created with restrictive modes (`700` on dirs, `600` on the DB file). Other unprivileged users on the same machine cannot read them.
- **Secrets via OS keychain.** API keys and credentials stored through `okuro keyring` go to the operating system's native keychain (macOS Keychain, Linux Secret Service / libsecret), not into the database. Memory dumps and DB leaks therefore don't expose them.
- **No network exposure by default.** The web dashboard binds to `127.0.0.1` and the MCP server uses local stdio. Nothing listens on a public interface unless you explicitly configure it.
- **Bootstrap-time and `okuro doctor` warning** when `~/.okuro/` is detected inside a git working tree that hasn't ignored it. The warning is loud; acting on it is yours.

## Out of scope (by design)

Okuro will not become any of these things in the current release line:

- **At-rest database encryption.** Adding SQLCipher would force a password prompt every session, defeating the always-on bootstrap UX. If you need at-rest encryption, full-disk encryption gives you the same protection without the friction.
- **Per-field encryption with user-rotated keys.** Same reasoning. Adds a key-management burden that single-user desktop tools shouldn't carry.
- **Redaction or sanitization of bootstrap content.** Okuro is designed to give agents your full context. If you want a sanitized profile, maintain a sanitized profile.
- **Multi-user hardening.** Okuro is a single-user tool. Running it under one user account, with one human at the keyboard, is the only supported configuration.
- **Cloud sync.** Not currently offered. If it is added, it will be end-to-end encrypted with a key the server cannot derive — never plain sync.

## Reporting a vulnerability

Open an issue on the okuro repo (https://github.com/saxmode/okuro-ai). For anything that involves leaked data or remote access, mark the issue private or email the maintainer directly before publishing details.
