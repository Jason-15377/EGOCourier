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

    def test_checkbox_draws_explicit_box_with_checkmark(self):
        """勾选框必须画出真正的方框，并在勾选时显示 ✓。

        两个坑各锁一条：
        - Windows 11 原生样式不画方框（未勾选只剩一条灰细线）——"方框不对劲"；
        - 只填背景不画勾，勾上之后是一块纯色方块（v1.0.8 的回归）。
        """
        self.assertIn("QCheckBox, QRadioButton { background:transparent;",
                      self.style)
        self.assertIn("QCheckBox::indicator { border-radius:3px;", self.style)
        self.assertIn("background:#ffffff; border:1px solid", self.style)
        self.assertIn('QCheckBox::indicator:checked { image:url("', self.style)
        # 勾上之后方框底色必须仍是白色
        checked_rule = self.style.split("QCheckBox::indicator:checked {", 1)[1]
        checked_rule = checked_rule.split("}", 1)[0]
        self.assertNotIn("background", checked_rule,
                         "勾选态不得改变方框底色，否则会变成实心色块")

    def test_checkmark_image_path_is_injected(self):
        """勾选图案必须换成真实存在的绝对路径。

        QSS 的 url() 不认 data: URI（实测静默失效，勾选态与未勾选态渲染一致），
        也不认相对路径，所以构建 STYLE 时要把 assets/check.svg 的绝对路径填进去。
        """
        self.assertNotIn("__CHECK_SVG__", self.style, "占位符未被替换")
        self.assertNotIn("data:image", self.style)
        image_url = self.style.split('image:url("', 1)[1].split('")', 1)[0]
        self.assertIn("/assets/check.svg", image_url.replace("\\", "/"))
        self.assertNotIn("\\", image_url, "QSS 里反斜杠会被当成转义字符")
        self.assertTrue(os.path.isfile(image_url), f"勾选图案不存在：{image_url}")

    def test_combo_popup_and_menu_are_styled(self):
        self.assertIn("QComboBox QAbstractItemView", self.style)
        self.assertIn("QMenu", self.style)
        self.assertIn("QToolTip", self.style)

    def test_scrollbars_are_styled(self):
        self.assertIn("QScrollBar::handle:vertical", self.style)

    def test_no_qt_internal_image_resources(self):
        """勾选态不引用 Qt 内部图片：跨版本路径不保证存在，失败会变成空框。"""
        self.assertNotIn(":/qt-project.org", self.style)


class TestCheckboxRendering(unittest.TestCase):
    """按像素验收勾选框：有方框边框、勾上之后能看出区别、不吃系统深色。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        main_mod._pin_light_appearance(cls.app)

    def _render(self, checked):
        """渲染一个勾选框，返回整张图的像素矩阵（行列表）。

        结构与真实界面一致：勾选框放在 QFrame#logTypeRow 卡片里。直接把勾选框
        挂在裸顶层 QWidget 上时，离屏平台会把它画成蓝色实心块（真实窗口不会），
        不能拿来验收。
        """
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QColor
        from PySide6.QtWidgets import (QCheckBox, QFrame, QMainWindow,
                                       QVBoxLayout, QWidget)

        win = QMainWindow()
        win.setStyleSheet(app_mod.STYLE)
        central = QWidget()
        win.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(4, 4, 4, 4)
        card = QFrame()
        card.setObjectName("logTypeRow")
        card.setAttribute(Qt.WA_StyledBackground, True)
        lay.addWidget(card)
        lay.addWidget(QWidget(), 1)          # 把卡片顶到左上，便于取样

        cb = QCheckBox("x")
        cb.setChecked(checked)
        QVBoxLayout(card).addWidget(cb)

        win.resize(160, 80)
        win.show()
        self.app.processEvents()
        self.app.processEvents()
        img = win.grab().toImage().convertToFormat(QImage.Format_RGB32)
        px = [[QColor(img.pixel(x, y)).name() for x in range(img.width())]
              for y in range(img.height())]
        win.close()
        return px

    @staticmethod
    def _box_bounds(px):
        """用灰边框色定位 indicator 四边，返回 (left, top, right, bottom)。"""
        border = "#a1a8b3"
        pts = [(x, y) for y, row in enumerate(px)
               for x, c in enumerate(row) if c == border]
        if not pts:
            raise AssertionError("找不到任何灰边框像素")
        return (min(x for x, _ in pts), min(y for _, y in pts),
                max(x for x, _ in pts), max(y for _, y in pts))

    @staticmethod
    def _is_dark(px, x, y):
        c = px[y][x].lstrip("#")
        return int(c[0:2], 16) < 120 and int(c[2:4], 16) < 120

    def test_box_has_visible_border(self):
        """未勾选时也要看得见方框，而不是一条灰细线。

        灰边色 #a1a8b3 在本界面只属于勾选框边框，用它定位 indicator 的四条边，
        再确认四边围出的区域内是白色填充。
        """
        px = self._render(False)
        left, top, right, bottom = self._box_bounds(px)
        border = "#a1a8b3"
        self.assertGreater(right - left, 8, "方框宽度异常")
        self.assertGreater(bottom - top, 8, "方框高度异常")
        # 四条边都要有连续的灰像素
        for name, seq in {
            "上": [px[top][x] for x in range(left + 2, right - 1)],
            "下": [px[bottom][x] for x in range(left + 2, right - 1)],
            "左": [px[y][left] for y in range(top + 2, bottom - 1)],
            "右": [px[y][right] for y in range(top + 2, bottom - 1)],
        }.items():
            solid = sum(1 for c in seq if c == border)
            self.assertGreaterEqual(solid, len(seq) - 2,
                                    f"{name}边框不连续（{solid}/{len(seq)}）")
        # 框内是白色填充
        cx, cy = (left + right) // 2, (top + bottom) // 2
        self.assertEqual(px[cy][cx], "#ffffff", "方框内部没有白色填充")

    def test_checked_differs_from_unchecked(self):
        """勾选态必须画出 ✓，否则「勾上了」和「没勾上」长得一样。"""
        unchecked = self._render(False)
        checked = self._render(True)
        left, top, right, bottom = self._box_bounds(unchecked)
        dark_unchecked = sum(
            1 for y in range(top, bottom + 1) for x in range(left, right + 1)
            if self._is_dark(unchecked, x, y))
        dark_checked = sum(
            1 for y in range(top, bottom + 1) for x in range(left, right + 1)
            if self._is_dark(checked, x, y))
        self.assertEqual(dark_unchecked, 0,
                         f"未勾选时框内不该有深色笔画（{dark_unchecked} 个）")
        self.assertGreaterEqual(dark_checked, 8,
                                f"勾选后看不到 ✓（深色像素 {dark_checked} 个）")
        # 笔画要细：现场反馈 1.7 的描边"太粗"。14px 方框下深色像素明显超过
        # 20 个就是又变粗了，重新调 assets/check.svg 的 stroke-width 即可。
        self.assertLessEqual(dark_checked, 20,
                             f"✓ 的线条偏粗（深色像素 {dark_checked} 个）")

    def test_does_not_follow_dark_palette(self):
        """系统深色调色板下，勾选框像素必须与浅色下完全一致。"""
        from PySide6.QtGui import QColor, QPalette

        light_checked, light_unchecked = self._render(True), self._render(False)
        dark = QPalette()
        dark.setColor(QPalette.Window, QColor("#1e1e1e"))
        dark.setColor(QPalette.Base, QColor("#2b2b2b"))
        dark.setColor(QPalette.Button, QColor("#2b2b2b"))
        dark.setColor(QPalette.ButtonText, QColor("#ffffff"))
        dark.setColor(QPalette.Text, QColor("#ffffff"))
        dark.setColor(QPalette.WindowText, QColor("#ffffff"))
        self.app.setPalette(dark)
        try:
            self.assertEqual(self._render(True), light_checked)
            self.assertEqual(self._render(False), light_unchecked)
        finally:
            main_mod._pin_light_appearance(self.app)


if __name__ == "__main__":
    unittest.main(verbosity=2)
