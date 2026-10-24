# Always Learn

![Always Learn](assets/banner.png)

**Stop scrolling while the AI thinks.** Every prompt you send, mid-vibe-coding or
mid-chat, comes with a wait, and that wait usually turns into a scroll. Always
Learn turns it into learning: **when you send a prompt, Anki pops up with a card.
When the AI finishes answering, Anki hides and you're back where you were.**

Works with ChatGPT, Claude, Gemini, Copilot, Perplexity, DeepSeek, Grok and more
in any major browser or desktop app, and with the AI chat inside **VS Code**
(Claude Code, Copilot), **Cursor**, **Windsurf**, **Trae** and **Kiro**.

![Demo: send a prompt, Anki pops up, then hides when the answer is ready](assets/screenshots/demo.gif)

<img src="assets/screenshots/settings.png" width="420" alt="Always Learn settings window">

## Install

First install [Anki](https://apps.ankiweb.net/) and its
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on (in Anki:
**Tools → Add-ons → Get Add-ons…**, code `2055492159`), then restart Anki.

**Windows:** download **always-learn.exe** from the
[Releases page](https://github.com/EduSR9/always-learn/releases) and
double-click it. If Windows warns about an unknown app, click
**More info → Run anyway**.

**macOS (beta):** download **always-learn-mac-beta.dmg** from the
[Releases page](https://github.com/EduSR9/always-learn/releases), drag
*Always Learn* into Applications, then right-click it → **Open** the first
time. Allow it under System Settings → Privacy & Security →
**Accessibility** and **Input Monitoring**. The Mac version is new, so please
[report any problems](https://github.com/EduSR9/always-learn/issues).

Then pick your deck and click **Try it: open Anki now**.

## Using it

| | |
|---|---|
| **Pause / Resume** | Stops Anki from popping up for a while. |
| **One deck / Random** | Review one deck, or rotate between several. |
| **Hide Anki when the AI is done** | Hides Anki as soon as the answer is ready. |
| **Start with Windows / at login** | Starts in the tray at login and opens Anki in the background. |

Closing the window keeps Always Learn running in the tray (Windows) or menu
bar (macOS). Click the robot icon to pause or quit.

## Troubleshooting

- **Nothing happens:** open the window and switch to your AI tab. The status
  should say `✓ AI chat`. If it doesn't, add the site to `ai_sites` in
  `~/.always-learn.json`.
- **"Open Anki to load your decks":** Anki or AnkiConnect isn't running.
- **"No card appeared":** the deck has no cards due today.
- **Anki doesn't hide after the answer:** keep the AI tab selected in its
  browser window while it answers.
- **macOS asks for permissions again after an update:** turn Always Learn off
  and on again in Privacy & Security.

## Run from source

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
.venv/bin/python always-learn.py                 # Windows: .venv\Scripts\pythonw always-learn.py
```

`build_exe.bat` (Windows) and `build_mac.sh` (macOS) build the installers;
GitHub Actions builds both on every release tag.

## Privacy

Everything runs on your computer. Always Learn reads the active window's name
and browser address, and only checks whether Enter was pressed, never what you
type. It talks only to AnkiConnect on `127.0.0.1`.

## License

[MIT](LICENSE)
