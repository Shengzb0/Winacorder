"""Retro 16-bit workstation stylesheet for the desktop application.

The palette is intentionally small. Hard outlines and offset bottom/right
borders provide a readable pixel-workbench hierarchy without relying on
rounded cards or decorative game textures.
"""

LIGHT_STYLESHEET = """
/* Core canvas ----------------------------------------------------------- */
QMainWindow, QDialog {
    background: #DDE5DE;
    color: #17233B;
    font-family: "Microsoft YaHei UI";
    font-size: 9.5pt;
}
QWidget {
    color: #17233B;
    font-family: "Microsoft YaHei UI";
    font-size: 9.5pt;
}
QWidget#centralCanvas { background: #DDE5DE; }
QLabel { background: transparent; }

/* Top command workstation --------------------------------------------- */
QFrame#appHeader {
    background: #F5F2E8;
    border: 2px solid #17233B;
    border-right: 5px solid #17233B;
    border-bottom: 5px solid #17233B;
    border-radius: 0px;
}
QWidget#brandBlock, QWidget#commandDeck,
QWidget#fileDeck, QWidget#taskDeck { background: transparent; }
QLabel#brandMark {
    background: transparent;
    color: #17233B;
    border: 0px;
    border-radius: 0px;
    font-family: "Cascadia Mono SemiBold", "Cascadia Mono";
    font-size: 14pt;
    font-weight: 700;
}
QLabel#brandTitle, QLabel#workspaceTitle {
    color: #17233B;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-size: 11pt;
    font-weight: 700;
}
QLabel#brandVersion {
    background: #CDEFF3;
    color: #06717D;
    border: 1px solid #17233B;
    padding: 1px 4px;
    font-family: "Cascadia Mono SemiBold", "Cascadia Mono";
    font-size: 7.5pt;
    font-weight: 700;
}
QLabel#brandSubtitle {
    color: #556377;
    font-size: 8.5pt;
}
QLabel#microLabel {
    color: #17233B;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 1px;
}

/* Pixel buttons: the thicker lower edge is the stepped shadow. -------- */
QToolButton, QPushButton {
    min-height: 25px;
    background: #FFFDF5;
    color: #17233B;
    border: 2px solid #17233B;
    border-right: 4px solid #17233B;
    border-bottom: 4px solid #17233B;
    border-radius: 0px;
    padding: 5px 9px 7px 9px;
    font-weight: 600;
}
QToolButton:hover, QPushButton:hover {
    background: #BFEFF3;
    border-color: #17233B;
}
QToolButton:pressed, QPushButton:pressed {
    background: #91DEE7;
    border-right-width: 2px;
    border-bottom-width: 2px;
    padding: 7px 7px 5px 11px;
}
QToolButton:focus, QPushButton:focus {
    background: #DDF9FA;
    border-color: #087C8A;
}
QToolButton[variant="file"] { background: #E9F0E8; }
QToolButton[variant="record"] { background: #FFE0D9; }
QToolButton[variant="stop"], QPushButton[role="danger"],
QPushButton[danger="true"] { background: #FFD8D4; }
QToolButton[variant="play"] { background: #CFF3E1; }
QToolButton[variant="pause"] { background: #F7E6AD; }
QToolButton[variant="click"], QPushButton[role="picker"] {
    background: #CDEFF3;
}
QToolButton[variant="emergency"] {
    background: #F06B62;
    color: #17233B;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-weight: 700;
}
QToolButton[variant="emergency"]:hover { background: #FF8D82; }
QPushButton[role="primary"] {
    background: #36C5D8;
    color: #17233B;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-weight: 700;
}
QPushButton[role="primary"]:hover { background: #64D7E4; }
QToolButton:disabled, QPushButton:disabled,
QToolButton[variant="file"]:disabled,
QToolButton[variant="record"]:disabled,
QToolButton[variant="stop"]:disabled,
QToolButton[variant="play"]:disabled,
QToolButton[variant="pause"]:disabled,
QToolButton[variant="click"]:disabled,
QToolButton[variant="emergency"]:disabled,
QPushButton[role="primary"]:disabled,
QPushButton[role="picker"]:disabled,
QPushButton[role="danger"]:disabled,
QPushButton[danger="true"]:disabled {
    color: #858D96;
    background: #D5D8D2;
    border-color: #858D96;
}

/* Notice strip --------------------------------------------------------- */
QFrame#privacyBanner {
    background: #F6E3A6;
    border: 2px solid #17233B;
    border-right: 4px solid #17233B;
    border-bottom: 4px solid #17233B;
    border-radius: 0px;
}
QLabel[tone="warning"] { color: #493C21; font-weight: 600; }
QLabel[tone="muted"] { color: #556377; }

/* Main workspace ------------------------------------------------------- */
QFrame#workspacePaper {
    background: #F5F2E8;
    border: 2px solid #17233B;
    border-right: 5px solid #17233B;
    border-bottom: 5px solid #17233B;
    border-radius: 0px;
}
QLabel[badge="true"] {
    background: #DDF9FA;
    color: #17233B;
    border: 2px solid #17233B;
    border-radius: 0px;
    padding: 3px 7px;
    font-family: "Cascadia Mono SemiBold", "Cascadia Mono";
    font-weight: 700;
}

/* Right mode console --------------------------------------------------- */
QTabWidget#settingsTabs::pane {
    background: #F5F2E8;
    border: 2px solid #17233B;
    border-right: 5px solid #17233B;
    border-bottom: 5px solid #17233B;
    border-radius: 0px;
    top: -2px;
}
QTabWidget#settingsTabs QTabBar::tab {
    min-width: 44px;
    background: #CDD8D1;
    color: #465367;
    border: 2px solid #17233B;
    border-bottom: 4px solid #17233B;
    border-radius: 0px;
    padding: 7px 7px 9px 7px;
    margin-right: 2px;
    font-weight: 600;
}
QTabWidget#settingsTabs QTabBar::tab:selected {
    background: #36C5D8;
    color: #17233B;
    border-bottom: 2px solid #F5F2E8;
    padding-bottom: 10px;
    font-weight: 700;
}
QTabWidget#settingsTabs QTabBar::tab:hover:!selected {
    background: #E8EFE9;
    color: #17233B;
}
QTabWidget#settingsTabs QTabBar::tab:focus:!selected {
    background: #DDF9FA;
    color: #17233B;
    border-color: #087C8A;
}
QWidget#settingsPage, QWidget#customPagePlaceholder { background: transparent; }
QLabel#pageIntro {
    color: #465367;
    padding: 4px 2px 3px 2px;
    font-size: 8.5pt;
}
QLabel#placeholderTitle {
    color: #17233B;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-size: 11pt;
    font-weight: 700;
}
QFrame#placeholderTerminal {
    background: #17233B;
    border: 2px solid #17233B;
    border-right: 4px solid #36C5D8;
    border-bottom: 4px solid #36C5D8;
}
QFrame#placeholderTerminal QLabel,
QLabel#placeholderTerminal {
    color: #67D5A5;
    font-family: "Cascadia Mono";
}

/* Custom macro workbench ---------------------------------------------- */
QWidget#macroEditorPane {
    background: #E8EEE8;
    color: #17233B;
}
QScrollArea#macroEditorScrollArea,
QScrollArea#macroEditorScrollArea QWidget#qt_scrollarea_viewport,
QWidget#macroEditorCanvas {
    background: #E8EEE8;
    border: none;
}
QFrame#macroSchemeBar {
    background: #F5F2E8;
    border: 2px solid #17233B;
    border-right: 5px solid #17233B;
    border-bottom: 5px solid #17233B;
    border-radius: 0px;
}
QFrame#macroCommandBuilder {
    background: #FFFDF5;
    border: 2px solid #17233B;
    border-right: 6px solid #17233B;
    border-bottom: 6px solid #17233B;
    border-radius: 0px;
}
QFrame#macroCommandBuilder:disabled {
    background: #E2E6E0;
    border-color: #858D96;
}
QFrame#macroBuilderHeader {
    background: #36C5D8;
    border: none;
    border-bottom: 3px solid #17233B;
}
QFrame#macroCommandBuilder:disabled QFrame#macroBuilderHeader {
    background: #BFC9C2;
    border-bottom-color: #858D96;
}
QLabel#macroBuilderTitle {
    color: #17233B;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-size: 9.5pt;
    font-weight: 700;
    letter-spacing: 1px;
}
QPushButton#macroBuilderToggle {
    min-width: 48px;
    min-height: 19px;
    padding: 2px 6px 4px 6px;
    background: #F5F2E8;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-size: 8pt;
}
QWidget#macroBuilderContent,
QWidget#macroBuilderParameters,
QWidget#macroBuilderField,
QWidget#macroBuilderPreviews,
QWidget#macroBuilderActions {
    background: transparent;
}
QLabel#macroBuilderFieldLabel,
QLabel#macroBuilderCaption {
    color: #465367;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-size: 7.5pt;
    font-weight: 700;
    letter-spacing: 1px;
}
QComboBox#macroMouseButton {
    background: #DDF9FA;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-weight: 700;
}
QLabel#macroBuilderLineHint {
    padding: 3px 6px;
    border-left: 4px solid #858D96;
    color: #556377;
    font-size: 8pt;
}
QLabel#macroBuilderLineHint[state="synced"] {
    background: #DDF5E8;
    border-left-color: #2E9E78;
    color: #244C3E;
}
QLabel#macroBuilderLineHint[state="limited"] {
    background: #F6E3A6;
    border-left-color: #B98221;
    color: #493C21;
}
QLabel#macroBuilderFlow {
    background: #17233B;
    color: #67D5A5;
    border: 2px solid #17233B;
    border-right: 4px solid #36C5D8;
    border-bottom: 4px solid #36C5D8;
    border-radius: 0px;
    padding: 5px 7px 7px 7px;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-size: 8.5pt;
    font-weight: 700;
}
QLabel#macroBuilderPreview {
    background: #EDF4EE;
    color: #17233B;
    border: 2px solid #17233B;
    border-right: 4px solid #67D5A5;
    border-bottom: 4px solid #67D5A5;
    border-radius: 0px;
    padding: 5px 7px 7px 7px;
    font-family: "Cascadia Mono";
    font-size: 8.5pt;
}
QPushButton#macroBuilderInsert {
    background: #36C5D8;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-weight: 700;
}
QPushButton#macroBuilderUpdate {
    background: #67D5A5;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-weight: 700;
}
QPushButton#macroBuilderUpdate:disabled,
QPushButton#macroBuilderInsert:disabled {
    color: #858D96;
    background: #D5D8D2;
    border-color: #858D96;
}
QLabel#pixelCaption {
    color: #17233B;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-size: 9pt;
    font-weight: 700;
    letter-spacing: 1px;
}
QComboBox#macroSchemeCombo {
    min-width: 150px;
    background: #DDF9FA;
    color: #17233B;
    border: 2px solid #17233B;
    border-right: 4px solid #17233B;
    border-bottom: 4px solid #17233B;
    border-radius: 0px;
    font-family: "Cascadia Mono", "Microsoft YaHei UI";
    font-weight: 600;
}
QComboBox#macroSchemeCombo:focus { border-color: #087C8A; }
QComboBox#macroSchemeCombo:disabled {
    color: #858D96;
    background: #D5D8D2;
    border-color: #858D96;
}
QPlainTextEdit#macroSourceEditor {
    background: #FFFDF5;
    color: #17233B;
    border: 3px solid #17233B;
    border-right: 7px solid #36C5D8;
    border-bottom: 7px solid #36C5D8;
    border-radius: 0px;
    padding: 10px;
    selection-background-color: #36C5D8;
    selection-color: #17233B;
    font-family: "Cascadia Mono";
    font-size: 10pt;
}
QPlainTextEdit#macroSourceEditor:focus {
    background: #FFFFFF;
    border-color: #087C8A;
    border-right-color: #17233B;
    border-bottom-color: #17233B;
}
QPlainTextEdit#macroSourceEditor[readOnly="true"] {
    background: #E9ECE6;
    color: #556377;
    border-right-color: #858D96;
    border-bottom-color: #858D96;
}
QFrame#macroHelpBar {
    background: #F6E3A6;
    border: 2px solid #17233B;
    border-right: 4px solid #17233B;
    border-bottom: 4px solid #17233B;
    border-radius: 0px;
}
QLabel#macroHelpText {
    color: #493C21;
    font-family: "Cascadia Mono", "Microsoft YaHei UI";
    font-size: 8.5pt;
}
QFrame#macroRunBar {
    background: #F5F2E8;
    border: 2px solid #17233B;
    border-right: 5px solid #17233B;
    border-bottom: 5px solid #17233B;
    border-radius: 0px;
}
QFrame#macroRunBar QPushButton[variant="run"] { background: #67D5A5; }
QFrame#macroRunBar QPushButton[variant="pause"] { background: #F6D66F; }
QFrame#macroRunBar QPushButton[variant="stop"] { background: #F06B62; }
QFrame#macroRunBar QPushButton[variant="run"]:disabled,
QFrame#macroRunBar QPushButton[variant="pause"]:disabled,
QFrame#macroRunBar QPushButton[variant="stop"]:disabled {
    color: #858D96;
    background: #D5D8D2;
    border-color: #858D96;
}
QLabel#macroValidationStatus {
    border: 2px solid #17233B;
    border-right: 4px solid #17233B;
    border-bottom: 4px solid #17233B;
    border-radius: 0px;
    padding: 5px 9px 7px 9px;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-weight: 700;
}
QLabel#macroValidationStatus[state="idle"] {
    background: #D8DFD9;
    color: #465367;
}
QLabel#macroValidationStatus[state="success"],
QLabel#macroValidationStatus[state="running"] {
    background: #67D5A5;
    color: #17233B;
}
QLabel#macroValidationStatus[state="paused"],
QLabel#macroValidationStatus[state="countdown"] {
    background: #F6D66F;
    color: #17233B;
}
QLabel#macroValidationStatus[state="error"] {
    background: #F06B62;
    color: #17233B;
}

QGroupBox {
    background: #FFFDF5;
    border: 2px solid #17233B;
    border-radius: 0px;
    margin-top: 17px;
    padding: 14px 9px 9px 9px;
    font-weight: 700;
}
QGroupBox[accent="sky"] { border-top: 5px solid #36C5D8; }
QGroupBox[accent="mint"] { border-top: 5px solid #67D5A5; }
QGroupBox[accent="peach"] { border-top: 5px solid #F06B62; }
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    padding: 0px 6px;
    color: #17233B;
    background: #FFFDF5;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
}
QFrame#autoClickFooter {
    background: #E8EEE8;
    border: none;
    border-top: 3px solid #17233B;
}

/* Action track --------------------------------------------------------- */
QTableView#action_table {
    background: #FFFDF5;
    alternate-background-color: #EAF0E9;
    border: 2px solid #17233B;
    border-radius: 0px;
    gridline-color: #AAB5AC;
    selection-background-color: #BFEFF3;
    selection-color: #17233B;
    outline: none;
}
QTableView#action_table::item {
    border: none;
    border-bottom: 1px solid #AAB5AC;
    padding: 4px;
}
QTableView#action_table::item:hover { background: #DDF9FA; }
QTableView#action_table::item:selected { background: #BFEFF3; }
QTableView#action_table::item:focus { border: 2px solid #087C8A; }
QHeaderView::section {
    background: #17233B;
    color: #F5F2E8;
    border: none;
    border-right: 2px solid #36C5D8;
    border-bottom: 3px solid #36C5D8;
    padding: 7px 4px;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-weight: 700;
}

/* Inputs --------------------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    min-height: 23px;
    background: #FFFDF5;
    color: #17233B;
    border: 2px solid #17233B;
    border-left: 4px solid #17233B;
    border-right: 3px solid #17233B;
    border-bottom: 3px solid #17233B;
    border-radius: 0px;
    padding: 4px 6px;
    selection-background-color: #36C5D8;
}
QSpinBox, QDoubleSpinBox {
    font-family: "Cascadia Mono SemiBold", "Cascadia Mono";
}
QComboBox QAbstractItemView {
    background: #FFFDF5;
    color: #17233B;
    selection-background-color: #BFEFF3;
    border: 2px solid #17233B;
    outline: none;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {
    background: #F0FBFA;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    background: #FFFFFF;
    border-color: #17233B;
    border-left-color: #087C8A;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled {
    color: #858D96;
    background: #D5D8D2;
    border-color: #858D96;
}
QCheckBox, QRadioButton { background: transparent; spacing: 7px; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 14px;
    height: 14px;
    background: #FFFDF5;
    border: 2px solid #17233B;
    border-radius: 0px;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: #36C5D8;
    border: 3px solid #17233B;
}
QCheckBox:focus, QRadioButton:focus {
    color: #06717D;
    background: #DDF9FA;
}
QCheckBox:focus::indicator, QRadioButton:focus::indicator {
    border-color: #087C8A;
}
QCheckBox:disabled, QRadioButton:disabled { color: #858D96; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {
    background: #D5D8D2;
    border-color: #858D96;
}

/* Explicit state blocks ------------------------------------------------ */
QLabel[state="idle"], QLabel[state="running"],
QLabel[state="paused"], QLabel[state="countdown"], QLabel[state="error"] {
    border: 2px solid #17233B;
    border-right: 4px solid #17233B;
    border-bottom: 4px solid #17233B;
    border-radius: 0px;
    padding: 4px 8px 6px 8px;
    font-family: "Cascadia Mono SemiBold", "Microsoft YaHei UI";
    font-weight: 700;
}
QLabel[state="idle"] { background: #D8DFD9; color: #465367; }
QLabel[state="running"] { background: #67D5A5; color: #17233B; }
QLabel[state="paused"], QLabel[state="countdown"] {
    background: #F6D66F;
    color: #17233B;
}
QLabel[state="error"] { background: #F06B62; color: #17233B; }

/* Scrolling and status rail ------------------------------------------- */
QScrollArea { background: transparent; border: none; }
QScrollArea QWidget#qt_scrollarea_viewport { background: transparent; }
QScrollBar:vertical {
    background: #CBD5CE;
    width: 12px;
    margin: 0px;
    border: 2px solid #17233B;
}
QScrollBar::handle:vertical {
    background: #36C5D8;
    border: 2px solid #17233B;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: #67D5A5; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: #CBD5CE;
}
QScrollBar:horizontal {
    background: #CBD5CE;
    height: 12px;
    border: 2px solid #17233B;
}
QScrollBar::handle:horizontal {
    background: #36C5D8;
    border: 2px solid #17233B;
    min-width: 28px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }

QStatusBar {
    background: #17233B;
    color: #F5F2E8;
    border-top: 4px solid #36C5D8;
    font-family: "Cascadia Mono", "Microsoft YaHei UI";
}
QStatusBar QLabel { color: #F5F2E8; }
QStatusBar QLabel[badge="true"] {
    background: #263552;
    color: #F5F2E8;
    border: 1px solid #36C5D8;
    padding: 2px 6px;
}
QStatusBar QLabel[state="idle"], QStatusBar QLabel[state="running"],
QStatusBar QLabel[state="paused"], QStatusBar QLabel[state="countdown"],
QStatusBar QLabel[state="error"] {
    border: 1px solid #F5F2E8;
    border-right: 3px solid #F5F2E8;
    border-bottom: 3px solid #F5F2E8;
    padding: 2px 6px 4px 6px;
}
QProgressBar {
    border: 2px solid #F5F2E8;
    border-radius: 0px;
    text-align: center;
    background: #263552;
    color: #F5F2E8;
    font-family: "Cascadia Mono";
}
QProgressBar::chunk { background: #67D5A5; }
QSplitter::handle { background: transparent; width: 8px; }
QSplitter::handle:hover {
    background: #36C5D8;
    border-left: 2px solid #17233B;
    border-right: 2px solid #17233B;
}
QToolTip {
    background: #FFFDF5;
    color: #17233B;
    border: 2px solid #17233B;
    padding: 5px;
    font-family: "Microsoft YaHei UI";
}
"""

__all__ = ["LIGHT_STYLESHEET"]
