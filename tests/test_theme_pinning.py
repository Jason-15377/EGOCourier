# -*- coding: utf-8 -*-
"""界面外观必须钉死在浅色，不跟随 Windows 深色模式。

现场问题：客户机开着深色模式时，启动器界面里的**日志勾选框和「选项」勾选框**
勾完之后跟着系统配色走，出现深色小方块，与旁边的白卡片不是一套配色。

成因：整套界面 QSS 是手写浅色，但
  1) 从来没声明过"只支持浅色"，系统深色配色会漏进没被 QSS 显式描述的原生子控件
     （勾选框的方框、下拉弹层、滚动条槽、菜单）；
  2) QSS 里根本没有任何 QCheckBox 规则，方框背景完全交给调色板。

这里锁住两件事：应用启动时把外观钉成浅色；QSS 里勾选框/弹层有显式配色。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ego_gui import app as app_mod  # noqa: E402
from ego_gui import main as main_mod  # noqa: E402


class TestLightAppearancePinned(unittest.TestCase):
    def test_pin_light_appearance_forces_light_palette(self):
        """钉成浅色后，即使系统配色是深色，应用调色板也必须是浅底深字。

        不直接断言 colorScheme()：offscreen 平台根本不实现它，永远返回
        Unknown，断言它会把测试写成"平台相关"。真正的效果体现在调色板上。
        """
        from PySide6.QtGui import QColor, QPalette
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        dark = QPalette()
        dark.setColor(QPalette.Window, QColor("#1e1e1e"))
        dark.setColor(QPalette.WindowText, QColor("#eeeeee"))
        dark.setColor(QPalette.Base, QColor("#252526"))
        app.setPalette(dark)

        main_mod._pin_light_appearance(app)

        window = app.palette().color(QPalette.Window)
        text = app.palette().color(QPalette.WindowText)
        self.assertGreater(window.lightness(), 200,
                           f"窗口底色仍是深色：{window.name()}")
        self.assertLess(text.lightness(), 80,
                        f"文字颜色仍是浅色：{text.name()}")

    def test_pin_is_idempotent_and_never_raises(self):
        """老版本 Qt 上没有 setColorScheme 时不能抛异常把启动打断。"""
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        main_mod._pin_light_appearance(app)      # 重复调用要幂等
        main_mod._pin_light_appearance(app)


class TestStylesheetCoversNativeWidgets(unittest.TestCase):
    """QSS 必须显式描述这些原生控件，否则它们会按系统深色调色板绘制。"""

    def setUp(self):
        self.style = app_mod.STYLE

    def test_checkbox_declares_background_but_keeps_native_indicator(self):
        """勾选框要有显式 background（摆脱深色调色板），但不能自定义 indicator。

        v1.0.8 曾把 `QCheckBox::indicator:checked` 写成蓝底实心块，勾上之后整个
        方框变成一片蓝色填充，与系统原生外观（白底 + 深色 ✓）差得很明显。
        方框交给系统绘制，才能保持用户熟悉的样式。
        """
        self.assertIn("QCheckBox", self.style)
        self.assertIn("QCheckBox, QRadioButton { background:transparent;",
                      self.style)
        self.assertNotIn("QCheckBox::indicator", self.style,
                         "不要自定义勾选框的方框：会把它画成实心填充块")
        self.assertNotIn("QRadioButton::indicator", self.style)

    def test_combo_popup_and_menu_are_styled(self):
        self.assertIn("QComboBox QAbstractItemView", self.style)
        self.assertIn("QMenu", self.style)
        self.assertIn("QToolTip", self.style)

    def test_scrollbars_are_styled(self):
        self.assertIn("QScrollBar::handle:vertical", self.style)

    def test_no_qt_internal_image_resources(self):
        """勾选态不引用 Qt 内部图片：跨版本路径不保证存在，失败会变成空框。"""
        self.assertNotIn(":/qt-project.org", self.style)


if __name__ == "__main__":
    unittest.main(verbosity=2)
