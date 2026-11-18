"""The user guide, the commands quick reference and the change log: built from NVDA's own Markdown with
NVDA's own converter, each page with Linda's notice at its top, and opened by the Help menu's three
items and by F1 in a dialog through the desktop's default browser, for which a stand-in xdg-open on
PATH writes down what it was asked to open.

The pages in the checkout are the ones tools/build-artifacts writes there, which the dev shell runs
once per checkout; the tests read them and build a copy of their own, as the Meson build does.

	nix develop . -c tests/linux/rig --bind SCRATCH python3 -m pytest --basetemp=SCRATCH/NAME tests/linux/test_userDocs.py
"""

import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import desktopSession
import harness
from desktopSession import DesktopSession

ROOT = harness.ROOT
USER_DOCS = ROOT / "user_docs"
PAGES = ("userGuide.html", "keyCommands.html", "changes.html")
NOTICE = (
	'<body>\n<div id="lindaNotice" role="note"><p>'
	"This is NVDA's own documentation, and Linda follows it. "
	"Where it says the NVDA key, Linda calls that key the Linda key. "
	"What it says about Windows, the installer and Windows programs does not apply to Linda."
	"</p></div>\n<h1 "
)
SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")


def buildArtifacts(*arguments: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		[sys.executable, str(ROOT / "tools" / "build-artifacts"), *arguments],
		capture_output=True,
		text=True,
		check=False,
		timeout=600,
	)


def fakeXdgOpen(tmpPath: Path) -> tuple[dict[str, str], Path]:
	"""An environment whose xdg-open writes each argument it is given as a line of the file returned."""
	fakeBin = tmpPath / "bin"
	fakeBin.mkdir()
	opened = tmpPath / "opened"
	xdgOpen = fakeBin / "xdg-open"
	xdgOpen.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {opened}\n', encoding="utf-8")
	xdgOpen.chmod(0o755)
	env = {"PATH": f"{fakeBin}:{os.environ['PATH']}", "XDG_CACHE_HOME": str(tmpPath / "cache")}
	return env, opened


def openedLines(opened: Path, count: int, timeout: float = 30) -> list[str]:
	deadline = time.monotonic() + timeout
	lines: list[str] = []
	while time.monotonic() < deadline and len(lines) < count:
		time.sleep(0.05)
		if opened.exists():
			lines = opened.read_text(encoding="utf-8").splitlines()
	return lines


def redirectTarget(url: str) -> str:
	"""Where the context help redirect at the file URL url sends the browser."""
	assert url.startswith("file:///"), url
	text = Path(url.removeprefix("file://")).read_text(encoding="utf-8")
	start = text.index('content="0;url=') + len('content="0;url=')
	return text[start : text.index('"', start)]


def test_checkoutHasEveryLanguagesPagesWithTheNotice():
	harness.requireRig()
	for page in PAGES:
		text = (USER_DOCS / "en" / page).read_text(encoding="utf-8")
		assert NOTICE in text, page
		assert text.count('id="lindaNotice"') == 1, page
	assert "<title>NVDA 2026.3.1 User Guide</title>" in (USER_DOCS / "en" / "userGuide.html").read_text(
		encoding="utf-8"
	)
	# Every language with a user guide in either form has its pages.
	languages = [d.name for d in USER_DOCS.iterdir() if d.is_dir()]
	assert len(languages) > 50
	for language in languages:
		for docType, pages in (("userGuide", ("userGuide", "keyCommands")), ("changes", ("changes",))):
			if any((USER_DOCS / language / f"{docType}.{kind}").is_file() for kind in ("md", "xliff")):
				for page in pages:
					text = (USER_DOCS / language / f"{page}.html").read_text(encoding="utf-8")
					assert NOTICE in text, f"{language}/{page}.html"
	assert (
		(USER_DOCS / "pl" / "userGuide.html")
		.read_text(encoding="utf-8")
		.startswith('<!DOCTYPE html>\n<html lang="pl"')
	)


def test_nvdasMarkdownAndXliffAreUntouched():
	harness.requireRig()
	done = subprocess.run(
		["git", "status", "--porcelain", "--untracked-files=all", "--", "user_docs"],
		cwd=ROOT,
		capture_output=True,
		text=True,
		check=True,
		env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
	)
	assert done.stdout == ""


def test_buildIntoADirectoryAsMesonDoesAndAgainChangesNothing(tmp_path: Path):
	harness.requireRig()
	arguments = (
		"--out",
		str(tmp_path / "artefacts"),
		"--docs-out",
		str(tmp_path / "docs"),
		"--doc-languages",
		"en,pl,he",
	)
	first = buildArtifacts(*arguments)
	assert first.returncode == 0, first.stderr
	# en and pl have both documents, he only the user guide.
	assert "documents: 5 built, 0 already current" in first.stdout, first.stdout
	for language, pages in (("en", PAGES), ("pl", PAGES), ("he", PAGES[:2])):
		for page in pages:
			built = (tmp_path / "docs" / language / page).read_text(encoding="utf-8")
			assert NOTICE in built, f"{language}/{page}"
			assert built == (USER_DOCS / language / page).read_text(encoding="utf-8"), f"{language}/{page}"
		for name in ("styles.css", "numberedHeadings.css", "favicon.ico"):
			assert (tmp_path / "docs" / language / name).is_file()
	assert not (tmp_path / "docs" / "he" / "changes.html").exists()
	again = buildArtifacts(*arguments)
	assert again.returncode == 0, again.stderr
	assert "documents: 0 built, 5 already current" in again.stdout, again.stdout


def test_xliffConversionIsUpstreamsOwn(tmp_path: Path):
	"""build-artifacts runs markdownTranslate's generateMarkdown with its unit lookups indexed; the
	Markdown is the same as markdownTranslate run as SCons runs it."""
	harness.requireRig()
	for docType in ("userGuide", "changes"):
		xliff = USER_DOCS / "pl" / f"{docType}.xliff"
		upstream = tmp_path / f"{docType}.upstream.md"
		indexed = tmp_path / f"{docType}.indexed.md"
		done = subprocess.run(
			[
				sys.executable,
				"source/markdownTranslate.py",
				"generateMarkdown",
				"-x",
				str(xliff),
				"-o",
				str(upstream),
			],
			cwd=ROOT,
			capture_output=True,
			text=True,
			check=False,
			timeout=300,
		)
		assert done.returncode == 0, done.stderr
		done = buildArtifacts("--xliff-to-markdown", str(xliff), str(indexed))
		assert done.returncode == 0, done.stderr
		assert indexed.read_bytes() == upstream.read_bytes(), docType


#: The steps of core.main that a child takes before it can import the GUI's modules, as in
#: test_osServices.
PRELUDE = """
import os

import globalVars
import config
from NVDAState import WritePaths

if not WritePaths.configDir:
	WritePaths.configDir = config.getUserDefaultConfigPath()
config.initConfigPath()
config.initialize()
import NVDAHelper  # noqa: F401
"""


def test_pagesAreInTheInterfaceLanguageWithEnglishAsTheFallback(tmp_path: Path):
	"""Each language's own page where there is one, the language without its region next, English
	last; and F1's redirect names the guide in the interface language, at the control's section."""
	env, opened = fakeXdgOpen(tmp_path)
	done = harness.runChild(
		tmp_path,
		PRELUDE
		+ textwrap.dedent(
			"""
			import languageHandler
			import documentationUtils
			from gui import contextHelp

			found = {}
			for language in ("en", "pl", "pt_BR", "de_CH", "he", "zh_HK", "kn"):
				languageHandler.setLanguage(language)
				found[language] = [
					os.path.relpath(documentationUtils.getDocFilePath(page), globalVars.appDir)
					for page in ("userGuide.html", "keyCommands.html", "changes.html")
				]
			languageHandler.setLanguage("pl")
			contextHelp.showHelp("GeneralSettingsLanguage")
			result = found
			"""
		),
		prepare="appDir",
		env={**env, "XDG_CONFIG_HOME": str(tmp_path / "xdgConfig")},
		recordLevel=logging.WARNING,
	)
	done.assertClean()
	assert done.value == {
		"en": [
			"../user_docs/en/userGuide.html",
			"../user_docs/en/keyCommands.html",
			"../user_docs/en/changes.html",
		],
		"pl": [
			"../user_docs/pl/userGuide.html",
			"../user_docs/pl/keyCommands.html",
			"../user_docs/pl/changes.html",
		],
		"pt_BR": [
			"../user_docs/pt_BR/userGuide.html",
			"../user_docs/pt_BR/keyCommands.html",
			"../user_docs/pt_BR/changes.html",
		],
		"de_CH": [
			"../user_docs/de_CH/userGuide.html",
			"../user_docs/de_CH/keyCommands.html",
			"../user_docs/de_CH/changes.html",
		],
		# Hebrew has a translated user guide and no translated change log.
		"he": [
			"../user_docs/he/userGuide.html",
			"../user_docs/he/keyCommands.html",
			"../user_docs/en/changes.html",
		],
		"zh_HK": [
			"../user_docs/en/userGuide.html",
			"../user_docs/en/keyCommands.html",
			"../user_docs/en/changes.html",
		],
		"kn": [
			"../user_docs/en/userGuide.html",
			"../user_docs/en/keyCommands.html",
			"../user_docs/en/changes.html",
		],
	}
	(url,) = openedLines(opened, 1)
	assert url == f"file://{tmp_path}/cache/linda/contextHelp.html"
	assert redirectTarget(url) == f"file://{USER_DOCS}/pl/userGuide.html#GeneralSettingsLanguage"


def test_helpMenuItemsAndF1OpenTheirPagesInTheBrowser(tmp_path: Path):
	"""From the keyboard, as a person would: each of the Help menu's three documentation items, then F1
	on the language list the settings dialog's General panel opens on, under the rig's desktop."""
	env, opened = fakeXdgOpen(tmp_path)
	with DesktopSession(tmp_path / "desktop") as desktop:
		app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
		assert app.stdout.readline() == "ready\n"
		desktopSession.activate(GTK[1])
		nvda = desktop.startNVDA(SESSION_CONFIG, env=env)
		desktop.expect(*AT_START, since=0, timeout=60)
		expected = []
		for letter, page in (("u", "userGuide.html"), ("q", "keyCommands.html"), ("n", "changes.html")):
			mark = desktop.mark()
			desktop.press("Insert+n")
			desktop.expect("Preferences sub Menu P 1 of …", since=mark)
			mark = desktop.mark()
			desktop.press("h")
			desktop.expect("User Guide U 1 of …", since=mark)
			mark = desktop.mark()
			desktop.press(letter)
			desktop.expect(AT_START[0], since=mark)
			expected.append(page)
			lines = openedLines(opened, len(expected))
			assert [os.path.basename(line) for line in lines] == expected, lines
			assert os.path.realpath(lines[-1]) == str(USER_DOCS / "en" / page), lines

		mark = desktop.mark()
		desktop.press("Insert+control+g")
		desktop.expect(
			"Linda Settings: General (normal configuration) dialog",
			"Linda Language (requires restart): combo box …",
			since=mark,
			timeout=10,
		)
		desktop.press("F1")
		lines = openedLines(opened, 4)
		assert len(lines) == 4, lines
		target = redirectTarget(lines[3])
		assert target.startswith("file:///") and target.endswith(
			"/user_docs/en/userGuide.html#GeneralSettingsLanguage"
		)
		assert os.path.realpath(target.removeprefix("file://").split("#")[0]) == str(
			USER_DOCS / "en" / "userGuide.html"
		)
		mark = desktop.mark()
		desktop.press("Escape")
		desktop.expect(AT_START[0], since=mark)
		desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
