# Always Learn

![Always Learn](assets/banner.png)

<p align="center">
  <a href="https://github.com/EduSR9/always-learn/releases/latest/download/always-learn.exe"><img src="https://img.shields.io/badge/Download_for-Windows-0078D6?style=for-the-badge&logo=windows11&logoColor=white" alt="Download for Windows" height="48"></a>
  &nbsp;
  <a href="https://github.com/EduSR9/always-learn/releases/latest/download/always-learn-mac-beta.dmg"><img src="https://img.shields.io/badge/Download_for-Mac_(beta)-111111?style=for-the-badge&logo=apple&logoColor=white" alt="Download for Mac (beta)" height="48"></a>
</p>
<p align="center"><sub>Free · Click a button and the download starts · <a href="#install-in-3-steps">How to install</a></sub></p>

**Stop scrolling while the AI thinks.** Every prompt you send, mid-vibe-coding or
mid-chat, comes with a wait, and that wait usually turns into a scroll. Always
Learn turns it into learning: **when you send a prompt, Anki pops up with a card.
When the AI finishes answering, Anki hides and you're back where you were.**

Works with ChatGPT, Claude, Gemini, Copilot, Perplexity, DeepSeek, Grok and more
in any major browser or desktop app, and with the AI chat inside **VS Code**
(Claude Code, Copilot), **Cursor**, **Windsurf**, **Trae**, **Kiro** and
**Obsidian** (Copilot plugin).

![Demo: send a prompt, Anki pops up, then hides when the answer is ready](assets/screenshots/demo.gif)

<img src="assets/screenshots/settings.png" width="420" alt="Always Learn settings window">

## Install in 3 steps

**1. Get Anki** (the free flashcard app), if you don't have it yet:
download it from [apps.ankiweb.net](https://apps.ankiweb.net/) and install it.

**2. Add AnkiConnect to Anki** (lets Always Learn open your cards):
open Anki → **Tools → Add-ons → Get Add-ons…** → paste the code
`2055492159` → **OK** → close and reopen Anki.

**3. Install Always Learn:**

<table>
<tr><th>🪟 Windows</th><th>🍎 Mac (beta)</th></tr>
<tr valign="top"><td>

1. Click **[Download for Windows](https://github.com/EduSR9/always-learn/releases/latest/download/always-learn.exe)**.
2. Open the downloaded file `always-learn.exe`.
3. If a blue "Windows protected your PC" box appears, click
   **More info**, then **Run anyway**. (It's shown for any new app
   that isn't from a big company.)

</td><td>

1. Click **[Download for Mac](https://github.com/EduSR9/always-learn/releases/latest/download/always-learn-mac-beta.dmg)**.
2. Open the downloaded file and drag **Always Learn** into
   the **Applications** folder.
3. Open Applications, **right-click** Always Learn → **Open** →
   **Open** (only needed the first time).
4. When asked, allow it in **System Settings → Privacy & Security**
   under **Accessibility** and **Input Monitoring**, then open it again.

</td></tr>
</table>

That's it: pick your deck in the window that opens and click
**Try it: open Anki now**. From now on, Anki pops up every time you send a
prompt to an AI.

> The Mac version is brand new. If something doesn't work,
> [tell me here](https://github.com/EduSR9/always-learn/issues/new) (a free
> GitHub account is needed).

## Using it

| | |
|---|---|
| **Pause / Resume** | Stops Anki from popping up for a while. |
| **One deck / Random** | Review one deck, or rotate between several. Random skips decks you've finished for today and never repeats a deck twice in a row. |
| **Presets** | In Random, tick decks (press and drag to tick many at once), type a name like "Chinese" and click **Save preset**. Pick it from the list later to switch. |
| **Hide Anki when the AI is done** | Hides Anki as soon as the answer is ready. |
| **Play a sound when Anki opens** | A soft chime when your card pops up. Turn it off for silence. |
| **Start with Windows / at login** | Starts right after you sign in and opens Anki in the background. |

Closing the window keeps Always Learn running in the tray (Windows) or menu
bar (macOS). Click the robot icon to pause or quit.

## Troubleshooting

- **Nothing happens:** open the window and switch to your AI tab. The status
  should say `✓ AI chat`. If it doesn't, add the site to `ai_sites` in
  `~/.always-learn.json`.
- **"Open Anki to load your decks":** Anki or AnkiConnect isn't running.
- **"No card appeared":** the deck has no cards due today. In Random mode
  Always Learn skips finished decks by itself.
- **AI inside an editor (VS Code, Cursor, Obsidian) doesn't trigger it:** the
  AI's chat panel must be visible, since Always Learn watches for its Stop
  button to know the answer has started.
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
