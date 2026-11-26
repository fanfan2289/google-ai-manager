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
    {"id": "flow", "name": "Google Flow AI 积分", "allowance": "1000 积分", "period": "每月",
     "category": "ai_quota", "provider": "flow_credits", "status": "tracked",
     "url": "https://flow.google.com/", "note": "实验性：需在设置中开启，并在桌面端账号卡片上「连接 Flow」"},
    {"id": "ai_credits", "name": "AI 积分余额", "allowance": "随方案发放", "period": "每月",
     "category": "ai_quota", "provider": None, "status": "manual",
     "url": "https://one.google.com/ai/activity", "note": "仅能在 Google One 页面查看"},
    {"id": "gcp_credits", "name": "Google Cloud 赠金（开发者计划）", "allowance": "$10", "period": "每月",
     "category": "cloud", "provider": "gcp_credits", "status": "tracked",
     "url": "https://developers.google.com/program/my-benefits",
     "note": "实验性：需在设置中开启，并在桌面端账号卡片上「连接」；每笔赠金自发放起 12 个月有效"},
    {"id": "firebase_studio", "name": "Firebase Studio 工作区", "allowance": "30 个", "period": "订阅期内",
     "category": "cloud", "provider": None, "status": "manual",
     "url": "https://studio.firebase.google.com/"},
    {"id": "jules", "name": "Jules 异步编程助手", "allowance": "100 个任务（同时 15 个）", "period": "每天",
     "category": "ai_quota", "provider": None, "status": "manual",
     "url": "https://jules.google.com/", "note": "用 GitHub 仓库时才有用"},
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
    "https://labs.google/",
    "https://flow.google.com/",
    "https://developers.google.com/",
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
