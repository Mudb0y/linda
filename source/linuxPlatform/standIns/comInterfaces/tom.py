"""Stand-in for comInterfaces.tom.

On Windows NVDA's build generates this module from the Text Object Model type library in
msftedit.dll. Linux has neither, so only what modules read from it at import is here: the
ITextDocument interface, which nothing can obtain, and the text unit constants with their values
from tom.h. Any other name read from it is an AttributeError.
"""

from comtypes import GUID
from comtypes.automation import IDispatch


class ITextDocument(IDispatch):
	_iid_ = GUID("{8CC497C0-A1DF-11CE-8098-00AA0047BE5D}")


tomCharacter = 1
tomWord = 2
tomSentence = 3
tomParagraph = 4
tomLine = 5
tomStory = 6
