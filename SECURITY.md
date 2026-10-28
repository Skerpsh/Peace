# Security policy

PEACE handles a Telegram bot token, a Telegram user session (full access to that account)
and a list of subscribers, so security reports matter.

## Reporting a vulnerability

**Do not open a public issue.** Report it privately through GitHub:
**Security** tab → **Report a vulnerability**
([direct link](https://github.com/Skerpsh/Peace/security/advisories/new)).

Please include:

- what an attacker can do (send messages as the bot, read or change subscribers, make the bot
  show a false alert or a false all-clear, take over the userbot account, crash the bot, ...)
  and what they need for it (a message in a source channel, a chat with the bot, network access, ...);
- steps to reproduce;
- the affected part: parser, aggregation, bot, deploy scripts.

You should get an answer within a few days.

## Scope

Especially interesting:

- ways to make the bot announce a **false all-clear** or hide a real alert;
- HTML or link injection through channel text into bot messages;
- admin commands reachable by other chats;
- leaks of `.env`, the session file, backups or the database.

Known limitations, by design:

- PEACE trusts what its source channels publish. A compromised or mistaken source with enough
  weight can cause a wrong alert; all-clears need an official source or several channels agreeing.
- It is not an official warning system and has had no independent audit.
