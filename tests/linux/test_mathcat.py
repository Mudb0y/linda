"""Exercise Linda's packaged MathCAT provider with the Linux runtime."""

from pathlib import Path

import harness


def test_mathcatRegistersAndPresentsMath(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import NVDAHelper
		import gui
		import mathPres
		import libmathcat_py
		from mathPres.MathCAT import localization
		from mathPres.MathCAT._navNodeMapping import prepareMathMlForNavigation

		mathPres.initialize()
		assert mathPres.speechProvider
		assert mathPres.brailleProvider
		assert mathPres.interactionProvider
		assert localization.getSpeechStyles("en")
		math = "<math><mn>2</mn><mo>+</mo><mn>3</mn></math>"
		assert prepareMathMlForNavigation(math, object()) == (math, {})
		braille = mathPres.brailleProvider.getBrailleForMathMl(math)
		libmathcat_py.SetPreference("TTS", "None")
		libmathcat_py.SetMathML(math)
		result = {"speech": libmathcat_py.GetSpokenText(), "braille": braille}
		""",
	)
	done.assertClean()
	assert "2 plus 3" in done.value["speech"]
	assert done.value["braille"].strip()
