"""AT-SPI role and state names mapped onto controlTypes, as NVDA presents the same controls on Windows.

Roles and states arrive as libatspi's names (get_role_name, StateType nicks), which are also what the
engine will carry, so the tables stay the same whichever client stands behind them.
"""

from controlTypes import Role, State

#: Roles that map one to one, whatever the toolkit.
ROLES: dict[str, Role] = {
	"accelerator label": Role.STATICTEXT,
	"alert": Role.ALERT,
	"animation": Role.ANIMATION,
	"application": Role.APPLICATION,
	"article": Role.ARTICLE,
	"audio": Role.AUDIO,
	"block quote": Role.BLOCKQUOTE,
	"button": Role.BUTTON,
	"calendar": Role.CALENDAR,
	"canvas": Role.CANVAS,
	"caption": Role.CAPTION,
	"chart": Role.CHART,
	"check box": Role.CHECKBOX,
	"check menu item": Role.CHECKMENUITEM,
	"color chooser": Role.COLORCHOOSER,
	"column header": Role.TABLECOLUMNHEADER,
	"combo box": Role.COMBOBOX,
	"comment": Role.COMMENT,
	"content deletion": Role.DELETED_CONTENT,
	"content insertion": Role.INSERTED_CONTENT,
	"date editor": Role.DATEEDITOR,
	"definition": Role.DEFINITION,
	"description list": Role.LIST,
	"description term": Role.LISTITEM,
	"description value": Role.DEFINITION,
	"desktop frame": Role.DESKTOPPANE,
	"desktop icon": Role.DESKTOPICON,
	"dial": Role.DIAL,
	"dialog": Role.DIALOG,
	"directory pane": Role.DIRECTORYPANE,
	"document email": Role.DOCUMENT,
	"document frame": Role.DOCUMENT,
	"document presentation": Role.DOCUMENT,
	"document spreadsheet": Role.DOCUMENT,
	"document text": Role.DOCUMENT,
	"document web": Role.DOCUMENT,
	"drawing area": Role.CANVAS,
	"editbar": Role.EDITBAR,
	"embedded": Role.EMBEDDEDOBJECT,
	"entry": Role.EDITABLETEXT,
	"file chooser": Role.FILECHOOSER,
	"font chooser": Role.FONTCHOOSER,
	"footer": Role.FOOTER,
	"footnote": Role.FOOTNOTE,
	"form": Role.FORM,
	"frame": Role.FRAME,
	"glass pane": Role.GLASSPANE,
	"grouping": Role.GROUPING,
	"header": Role.HEADER,
	"heading": Role.HEADING,
	"icon": Role.ICON,
	"image": Role.GRAPHIC,
	"image map": Role.IMAGEMAP,
	"info bar": Role.ALERT,
	"input method window": Role.INPUTWINDOW,
	"internal frame": Role.INTERNALFRAME,
	"landmark": Role.LANDMARK,
	"layered pane": Role.LAYEREDPANE,
	"level bar": Role.PROGRESSBAR,
	"link": Role.LINK,
	"list": Role.LIST,
	"list box": Role.LIST,
	"list item": Role.LISTITEM,
	"log": Role.SECTION,
	"mark": Role.MARKED_CONTENT,
	"marquee": Role.SECTION,
	"math": Role.MATH,
	"menu bar": Role.MENUBAR,
	"menu item": Role.MENUITEM,
	"notification": Role.ALERT,
	"option pane": Role.OPTIONPANE,
	"page": Role.PAGE,
	"page tab": Role.TAB,
	"page tab list": Role.TABCONTROL,
	"paragraph": Role.PARAGRAPH,
	# Windows presents a password field as an edit field with the protected state.
	"password text": Role.EDITABLETEXT,
	"popup menu": Role.POPUPMENU,
	"progress bar": Role.PROGRESSBAR,
	"push button": Role.BUTTON,
	"push button menu": Role.MENUBUTTON,
	"radio button": Role.RADIOBUTTON,
	"radio menu item": Role.RADIOMENUITEM,
	"rating": Role.SLIDER,
	"redundant object": Role.REDUNDANTOBJECT,
	"root pane": Role.ROOTPANE,
	"row header": Role.TABLEROWHEADER,
	"ruler": Role.RULER,
	"scroll bar": Role.SCROLLBAR,
	"scroll pane": Role.SCROLLPANE,
	"section": Role.SECTION,
	"separator": Role.SEPARATOR,
	"slider": Role.SLIDER,
	"spin button": Role.SPINBUTTON,
	"split pane": Role.SPLITPANE,
	"static": Role.STATICTEXT,
	"status bar": Role.STATUSBAR,
	"suggestion": Role.SUGGESTION,
	"switch": Role.SWITCH,
	"table": Role.TABLE,
	"table cell": Role.TABLECELL,
	"table column header": Role.TABLECOLUMNHEADER,
	"table row": Role.TABLEROW,
	"table row header": Role.TABLEROWHEADER,
	"tearoff menu item": Role.TEAROFFMENU,
	"terminal": Role.TERMINAL,
	"timer": Role.CLOCK,
	"title bar": Role.TITLEBAR,
	"toggle button": Role.TOGGLEBUTTON,
	"tool bar": Role.TOOLBAR,
	"tool tip": Role.TOOLTIP,
	"tree": Role.TREEVIEW,
	"tree item": Role.TREEVIEWITEM,
	"tree table": Role.TREEVIEW,
	"video": Role.VIDEO,
	"viewport": Role.VIEWPORT,
	"window": Role.WINDOW,
}

#: States that map one to one.
STATES: dict[str, State] = {
	"busy": State.BUSY,
	"checkable": State.CHECKABLE,
	"checked": State.CHECKED,
	"collapsed": State.COLLAPSED,
	"defunct": State.DEFUNCT,
	"editable": State.EDITABLE,
	"expanded": State.EXPANDED,
	"focusable": State.FOCUSABLE,
	"focused": State.FOCUSED,
	"has-popup": State.HASPOPUP,
	"iconified": State.ICONIFIED,
	"invalid-entry": State.INVALID_ENTRY,
	"modal": State.MODAL,
	"multi-line": State.MULTILINE,
	"multiselectable": State.MULTISELECTABLE,
	"pressed": State.PRESSED,
	"read-only": State.READONLY,
	"required": State.REQUIRED,
	"selectable": State.SELECTABLE,
	"selected": State.SELECTED,
	"supports-autocompletion": State.AUTOCOMPLETE,
	"truncated": State.CROPPED,
	"visited": State.VISITED,
}

_HASPOPUP_KINDS = {
	"dialog": State.HASPOPUP_DIALOG,
	"grid": State.HASPOPUP_GRID,
	"listbox": State.HASPOPUP_LIST,
	"tree": State.HASPOPUP_TREE,
}


def mapStates(raw: frozenset[str], role: Role, attributes: dict[str, str]) -> set[State]:
	"""NVDA's states for an object, with the derived and inverted ones IAccessible2 also derives."""
	states = {STATES[s] for s in raw if s in STATES}
	# Either state means usable, as GTK 4 never sets enabled; Caja's icons carry neither and are usable.
	if "sensitive" not in raw and "enabled" not in raw and role != Role.ICON:
		states.add(State.UNAVAILABLE)
	if "visible" not in raw:
		states.add(State.INVISIBLE)
	elif "showing" not in raw:
		states.add(State.OFFSCREEN)
	if "expandable" in raw and "expanded" not in raw:
		states.add(State.COLLAPSED)
	if "indeterminate" in raw:
		if role in (Role.CHECKBOX, Role.CHECKMENUITEM):
			states.add(State.HALFCHECKED)
		else:
			states.add(State.INDETERMINATE)
	if role == Role.TOGGLEBUTTON:
		# GTK 3 reports an active toggle button as checked; NVDA, like IAccessible2, says pressed.
		if State.CHECKED in states:
			states.discard(State.CHECKED)
			states.add(State.PRESSED)
		states.discard(State.CHECKABLE)
	if role == Role.PASSWORDEDIT:
		states.add(State.PROTECTED)
	kind = attributes.get("haspopup")
	if State.HASPOPUP in states and kind in _HASPOPUP_KINDS:
		states.discard(State.HASPOPUP)
		states.add(_HASPOPUP_KINDS[kind])
	sort = attributes.get("sort")
	if sort == "ascending":
		states.add(State.SORTED_ASCENDING)
	elif sort == "descending":
		states.add(State.SORTED_DESCENDING)
	elif sort == "other":
		states.add(State.SORTED)
	# The IAccessible2 rules, IAccessible/__init__.py _get_states.
	if role not in (Role.LIST, Role.LISTITEM) and State.READONLY in states:
		states.discard(State.EDITABLE)
	if State.HASPOPUP in states and State.AUTOCOMPLETE in states:
		states.discard(State.HASPOPUP)
	if State.HALFCHECKED in states:
		states.discard(State.CHECKED)
	return states
