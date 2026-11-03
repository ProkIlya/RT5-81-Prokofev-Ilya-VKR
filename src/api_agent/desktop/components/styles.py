"""Общие компактные размеры и семантические роли во всех окнах TUI."""
COMPACT_CSS='''
* {scrollbar-size: 1 1; scrollbar-background: $surface; scrollbar-background-hover: $surface; scrollbar-background-active: $surface;}
PopupMenu {background: transparent;}
PopupMenu OptionList {border: none; padding: 0;}
Button, PageDialog Button {height: 1; min-width: 3; padding: 0 1; border: none; margin-right: 1;}
Button:focus {text-style: bold reverse;}
Button.action-add {color: $success;}
Button.action-edit {color: $warning;}
Button.action-delete {color: $error;}
Input, PageDialog Input {height: 3; border: solid $foreground 40%; padding: 0 1; margin: 0 0 1 0;}
Input:focus {border: solid $accent; background: $boost;}
Select, PageDialog Select {height: 3; margin-bottom: 1;}
SelectCurrent {height: 3; border: solid $foreground 40%; padding: 0 1;}
Select:focus > SelectCurrent {border: solid $accent; background: $boost;}
SelectOverlay {max-height: 12;}
PageDialog .page-title {height: 1; margin-bottom: 1;}
PageDialog .page-actions, PageDialog .editor-actions {height: auto; padding-top: 1;}
PageDialog .field-help {margin-bottom: 1; color: $text-muted;}
FormDialog .form-field {height: auto; margin-bottom: 0; max-width: 90;}
FormDialog .form-path {color: $accent;}
FormDialog Input {margin-bottom: 0;}
FormDialog .form-field Label {color: $text-muted; margin-bottom: 0;}
FormDialog .form-field .field-help {margin-bottom: 0;}
#app-bar {height: 1;}
#app-title {height: 1; padding: 0 1;}
#app-bar Button {height: 1; min-width: 3; color: $text; background: $panel;}
#controls {height: 1;}
#inspector-head {height: 1;}
#inspector-label {height: 1; padding: 0; color: $accent;}
#inspector-close {height: 1; width: 3; min-width: 3; margin: 0;}
#conversation ListItem {background: transparent; padding: 0; margin: 0 0 2 0; border: none;}
#conversation ListItem.-highlight {background: transparent;}
#conversation ListItem > .bubble {background: $surface;}
#conversation {background: $surface;}
#conversation ListItem, #conversation ListItem.-highlight {background: $surface;}
#conversation .user-message {margin-left: 0; align-horizontal: right; border: none; background: $surface;}
#conversation .agent-message {margin-right: 0; align-horizontal: left; border: none; background: $surface;}
#conversation .bubble {height: auto; padding: 0 1; background: $surface; border: solid transparent;}
#conversation .user-message .bubble {border-right: solid $accent;}
#conversation .agent-message .bubble {border-left: solid $primary;}
#conversation .user-message.-highlight .bubble {border: solid $accent;}
#conversation .agent-message.-highlight .bubble {border: solid $primary;}
#conversation .message-actions {height: 1; margin-top: 1;}
#conversation .message-actions Button {height: 1; min-width: 3; padding: 0 1; color: $accent;}
#conversation .program-result {padding-top: 0;}
AssertionEditor .field-help {margin: 1 0;}
AssertionEditor .page-actions {padding-top: 1;}
CardEditor .editor-body Label {margin-bottom: 1;}
CardEditor #card-scope {background: $boost; color: $accent; border-left: solid $accent; border-right: solid $accent; text-style: bold; padding: 0 1;}
CardEditor #card-scope:hover, CardEditor #card-scope:focus {background: $primary-muted; color: $text;}
#goal {height: 4;}
Collapsible {padding: 0; border: none; margin: 0 0 1 0;}
Collapsible > Contents {padding: 0 1;}
.section-title {height: 1; text-style: bold; color: $accent; margin-bottom: 1;}
.settings-group {height: auto; width: 84; max-width: 100%; padding: 1 2; margin-bottom: 1; background: $panel;}
.settings-group Input {color: $accent;}
.-light-mode .page-title, .-light-mode .section-title {color: #17657f;}
.-light-mode #conversation .user-message .message-role {color: #9a6000;}
.-light-mode #conversation .agent-message .message-role {color: #17657f;}
.-light-mode #conversation .user-message .bubble {border-right: solid #9a6000;}
.-light-mode #conversation .agent-message .bubble {border-left: solid #17657f;}
.-light-mode #conversation .user-message.-highlight .bubble {border: solid #9a6000;}
.-light-mode #conversation .agent-message.-highlight .bubble {border: solid #17657f;}
'''
