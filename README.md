# 慕尼黑 IT 职位监控

每天自动抓取慕尼黑新发布的 **Cloud / DevOps / Platform / AI / Software Engineer** 职位,结果发布到网页。
面向 1 年左右工作经验:已过滤 Senior / Lead / Leiter / Architekt 等资深岗,以及 Data 类岗位。

判断"新"靠自己建库做差分:职位 ID 首次出现的那天才算新,不看平台标注的日期(StepStone 之类经常滞后)。

**网址**: https://hasi7758.github.io/jobwatch_IT_Munich/

## 数据来源
- **arbeitnow.com** — 德国公开职位接口,GitHub 服务器可达,按创建时间倒序抓 8 页,靠地点过滤留下慕尼黑及周边。
- **公司直连** — 40 多家公司的招聘系统接口(Celonis、Helsing、Personio、Infineon、SAP、Airbus、Bosch、
  Cloudflare、NVIDIA、Salesforce…)。公司发到自家系统永远早于聚合平台。
- **联邦劳动局**接口封了 GitHub 的 IP,默认关闭;在自己电脑上跑时把 `config.yaml` 里 `arbeitsagentur.enabled` 改 true 即可。

覆盖不到的:StepStone / LinkedIn / Indeed 独家发布的岗位,以及 BMW、Siemens、Allianz、Google 等
没有公开接口的公司(它们只能靠 arbeitnow 覆盖到一部分,或者直接去官网投)。

## 页面怎么看
- **点标签就能筛选**:顶部四组标签(方向 / 经验 / 语言 / 行业),同组内是"或",跨组是"且"。
  比如点「云/DevOps/平台」+「英文职位名」= 英语环境的云岗;职位卡片上的标签也能点。
- 搜索框按职位名/公司名过滤;「隐藏中介/派遣/工程服务岗」一键去掉 Hays / Ferchau / Brunel 等;「清除筛选」复位。
- 筛选状态写在网址 `#` 后面,可以把常用组合收藏成书签,比如 `#t=云/DevOps/平台&na=1`。
- 绿色 **初级友好** = 职位名含 Junior / Absolvent / Einsteiger / Trainee / Engineer I 等,排在最前面。
- 青色 **英文职位名** = 职位名是英文,英语工作环境概率高;有单独一节不限日期列出。
- 灰色 **招聘中介** 含猎头、派遣和工程服务商(Hays、Ferchau、Brunel、Alten…),都是合同岗,真实雇主另有其人。
- 列表排序:初级友好 → 直接雇主 → 中介,同组内新的在前。

定时:每天 UTC 05:40(慕尼黑夏令时 07:40 / 冬令时 06:40)。

## 改关键词
在 GitHub 网页上点开 `config.yaml`,编辑 `keywords.include` / `exclude`,提交即可。
- 只看职位名,整词匹配,自动兼容德语阴性后缀(`entwickler` 也命中 Entwicklerin / Entwickler:in)。
- 以 `~` 开头是词根,子串匹配:`~entwickl` 命中 Softwareentwickler / Webentwicklung / Anwendungsentwickler。
- `exclude` 优先级高于 `include`。
- 默认排除了 `embedded`(慕尼黑汽车业 embedded 岗海量,方向不同),想看就删那一行。

## 改标签规则
标签规则在 `jobwatch.py` 里的 `ROLE_RULES` / `JUNIOR_TERMS` / `CATEGORY_RULES`(公司名整词匹配)。
改完把 `TAGS_VERSION` 字符串换一下,下次运行会给库里全部职位重新打标签。

## 加公司
打开公司招聘页,点一个职位看地址栏跳到哪个域名,按 `companies.yaml` 末尾的对照表填进去。
慕尼黑创业公司大量用 Personio(`xxx.jobs.personio.de`),本地跑 `python jobwatch.py discover <slug>` 可自动探测。
`companies.yaml` C 段的公司是凭经验加的,首次运行后看 Actions 日志,标 `[失败]` 的删掉。

## 注意
GitHub 规定公开仓库连续 60 天无真人活动会停用定时任务,
届时会收到邮件,去 Actions 页面点一下 Enable workflow 即可。
