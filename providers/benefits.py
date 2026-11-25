"""Google AI Pro benefits catalog (权益中心).

Static allowances come from Google's public plan page and may change — the UI always links to the
official page. `provider` links a benefit to a live QuotaProvider; benefits without one have no
programmatic API and are shown with their official entry point.
"""

from __future__ import annotations

from typing import Any

PLANS_URL = "https://one.google.com/about/google-ai-plans/"

# tracked = live data from a provider; manual = only visible on Google's own page; planned = later phase
BENEFITS: list[dict[str, Any]] = [
    {"id": "storage", "name": "Google One 云端存储", "allowance": "5 TB（部分方案 10 TB）", "period": "订阅期内",
     "category": "storage", "provider": "google_storage", "status": "tracked",
     "url": "https://one.google.com/storage"},
    {"id": "colab", "name": "Colab 计算单元", "allowance": "200 CCU", "period": "每月",
     "category": "compute", "provider": "colab", "status": "tracked",
     "url": "https://colab.research.google.com/"},
    {"id": "antigravity", "name": "Google Antigravity", "allowance": "5 小时 + 每周双窗口", "period": "滚动窗口",
     "category": "ai_quota", "provider": "antigravity", "status": "tracked",
     "url": "https://antigravity.google/"},
    {"id": "gemini_cli", "name": "Gemini CLI / Code Assist", "allowance": "1500 次请求", "period": "每天",
     "category": "ai_quota", "provider": "gemini_cli", "status": "tracked",
     "url": "https://github.com/google-gemini/gemini-cli"},
    {"id": "flow", "name": "Google Flow AI 积分", "allowance": "1000 积分", "period": "每月",
     "category": "ai_quota", "provider": None, "status": "planned",
     "url": "https://labs.google/fx/tools/flow", "note": "无官方查询接口，Phase 2 实验性支持"},
    {"id": "ai_credits", "name": "AI 积分余额", "allowance": "随方案发放", "period": "每月",
     "category": "ai_quota", "provider": None, "status": "manual",
     "url": "https://one.google.com/ai/activity", "note": "仅能在 Google One 页面查看"},
    {"id": "gcp_credits", "name": "Google Cloud 赠金", "allowance": "$10", "period": "每月",
     "category": "cloud", "provider": None, "status": "manual",
     "url": "https://console.cloud.google.com/billing", "note": "在 Cloud 控制台「结算 → 赠送金额」查看"},
    {"id": "firebase_studio", "name": "Firebase Studio 工作区", "allowance": "30 个", "period": "订阅期内",
     "category": "cloud", "provider": None, "status": "manual",
     "url": "https://studio.firebase.google.com/"},
    {"id": "jules", "name": "Jules 异步编码代理", "allowance": "更高每日任务数", "period": "每天",
     "category": "ai_quota", "provider": None, "status": "planned",
     "url": "https://jules.google.com/", "note": "Phase 2：按 24 小时任务数统计"},
    {"id": "gemini_app", "name": "Gemini 应用（高级模型 / Deep Research）", "allowance": "以官方页面为准", "period": "",
     "category": "ai_quota", "provider": None, "status": "manual",
     "url": "https://gemini.google.com/"},
    {"id": "notebooklm", "name": "NotebookLM", "allowance": "更高笔记本与来源上限", "period": "",
     "category": "ai_quota", "provider": None, "status": "manual",
     "url": "https://notebooklm.google.com/"},
]

# Hosts the desktop shell may open in the system browser (also used for OAuth consent links).
EXTERNAL_URL_PREFIXES: tuple[str, ...] = (
    "https://accounts.google.com/",
    "https://one.google.com/",
    "https://colab.research.google.com/",
    "https://antigravity.google/",
    "https://github.com/google-gemini/",
    "https://labs.google/",
    "https://console.cloud.google.com/",
    "https://studio.firebase.google.com/",
    "https://jules.google.com/",
    "https://gemini.google.com/",
    "https://notebooklm.google.com/",
)


def catalog() -> list[dict[str, Any]]:
    return [dict(b) for b in BENEFITS]


def tracked_provider_ids() -> list[str]:
    return [b["provider"] for b in BENEFITS if b.get("provider")]
