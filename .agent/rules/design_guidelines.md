# UI & Design System Guidelines: Apple Native (HIG) Standards

## 1. 核心铁律：彻底去除“AI 味” (Zero AI Cliché)
- **严禁任何 Emoji 图标**：页面中绝对不允许出现 🚀、📈、💡、🔥、🤖、✨ 等表情符号。所有图标必须使用 Apple SF Symbols 风格的单色超细线性矢量图标。
- **严禁浮夸渐变与光晕**：禁用紫色/青色弥散渐变、发光边框（Glow Effect）、廉价玻璃拟态。UI 质感必须沉稳、克制。
- **严禁空洞话术**：杜绝“AI驱动”、“智慧赋能”、“一键极速感知”等营销辞藻，只保留客观、高密度的金融事件事实与传导链路数据。
- **严禁无意义的装饰卡片**：每一个组件必须有明确的信息呈现价值，杜绝为了好看而堆砌的空白卡片或装饰图表。

## 2. 视觉基准：Apple 股市 / HIG 原生设计标准
- **背景与层级 (Dark Mode Hierarchy)**:
  - Base: `#000000` (OLED 纯黑)
  - Secondary/Cards: `#1C1C1E` (iOS 经典分组背景)
  - Tertiary/Interactive: `#2C2C2E` (Apple 按钮/筛选器激活态)
  - Dividers: `rgba(255, 255, 255, 0.08)` 或 `0.5px solid #38383A`
- **排版与字阶 (Typography)**:
  - 字体族：`-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "PingFang SC", sans-serif`
  - 数字展示：必须开启 `font-variant-numeric: tabular-nums` (等宽对齐)，行情涨跌幅使用清晰的中度加粗 (Medium/Semibold)
  - 字阶对照：
    - 大标题：34px / Bold
    - 一级卡片标题：16px / Semibold
    - 正文描述：13px / Regular / text-neutral-300
    - 辅助信息/时间：11px / Regular / text-neutral-500
- **数据胶囊 (Pill Badges)**:
  - 严格参考 Apple Stocks：精简的背景色（如 `#2C2C2E` 或极低透明度的 Red/Green 预警底色），搭配精准的红绿变动文字，不喧宾夺主。
- **传导链路展现**:
  - 采用极简排印符号（如 `A → B → C`），以清晰的逻辑链条替代复杂的视觉插图。
