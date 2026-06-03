# A股主升交易台

这是一个可直接发布到 GitHub Pages 的静态网页。页面读取 `data/latest.json` 展示每日主升候选、买点/卖点计划、强势板块、新闻线索，并用浏览器 `localStorage` 记录用户交易。

## 核心逻辑

- 板块先过线：连续强势、涨停/大涨数量、成交额放大、龙头连续性、指数位置，满足 4 条以上。
- 个股再过线：成交额放大、突破结构、资金连续流入、均线结构、换手率、分时强度，满足 5 条以上。
- 买点只保留三类：弱转强突破、第一次分时回踩、回封板。
- 不满足条件时不硬凑推荐，页面显示空仓等待。

## 每日自动更新

`.github/workflows/update-data.yml` 会在 A 股交易日自动运行：

- 09:35 中国时间
- 09:50 中国时间
- 10:05 中国时间
- 15:20 中国时间

脚本会抓取东方财富行情、板块、K 线、资金流，并抓取东方财富、同花顺、第一财经的市场新闻线索，然后写入 `data/latest.json`。

首次放到 GitHub 后，请在仓库设置里确认：

- `Settings → Pages` 选择从主分支根目录发布。
- `Settings → Actions → General → Workflow permissions` 允许 `Read and write permissions`。
- 可在 `Actions → Update A-share recommendations → Run workflow` 手动触发第一次更新。

## 本地预览

在项目根目录启动任意静态服务器即可：

```bash
python -m http.server 8000
```

然后打开 `http://localhost:8000`。

## 数据来源

- 东方财富行情与资金数据：[quote.eastmoney.com](https://quote.eastmoney.com/)
- 同花顺个股与资讯：[10jqka.com.cn](https://www.10jqka.com.cn/)
- 第一财经资讯：[yicai.com](https://www.yicai.com/)

页面和脚本仅按 `sk.docx` 规则生成量化候选，用于交易研究和纪律执行，不构成个性化投资建议。
