"""Impact.com affiliate API client для AI Office."""
import os
import requests
from requests.auth import HTTPBasicAuth

IMPACT_BASE_URL = "https://api.impact.com"

def _get_auth():
    account_sid = os.getenv("IMPACT_ACCOUNT_SID")
    auth_token = os.getenv("IMPACT_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise ValueError("IMPACT_ACCOUNT_SID и IMPACT_AUTH_TOKEN не заданы в env")
    return HTTPBasicAuth(account_sid, auth_token), account_sid

def get_campaigns() -> list[dict] | None:
    """Получить список доступных кампаний брендов."""
    try:
        auth, sid = _get_auth()
        url = f"{IMPACT_BASE_URL}/Mediapartners/{sid}/Campaigns"
        resp = requests.get(url, auth=auth, params={"PageSize": 50})
        resp.raise_for_status()
        data = resp.json()
        return data.get("Campaigns") or data.get("campaigns") or []
    except Exception as e:
        print(f"[impact_client] get_campaigns error: {e}")
        return None

def get_ads(campaign_id: str = None, niche: str = None) -> list[dict] | None:
    """Получить список объявлений/офферов.
    
    Args:
        campaign_id: фильтр по ID кампании (опционально)
        niche: ключевое слово для фильтрации по нише (опционально)
    """
    try:
        auth, sid = _get_auth()
        url = f"{IMPACT_BASE_URL}/Mediapartners/{sid}/Ads"
        params = {"PageSize": 50}
        if campaign_id:
            params["CampaignId"] = campaign_id
        if niche:
            params["Niche"] = niche
        resp = requests.get(url, auth=auth, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("Ads") or data.get("ads") or []
    except Exception as e:
        print(f"[impact_client] get_ads error: {e}")
        return None

def get_tracking_link(ad_id: str) -> str | None:
    """Получить трекинг ссылку для конкретного оффера.
    
    Args:
        ad_id: ID объявления
    Returns:
        Трекинг URL строкой или None
    """
    try:
        auth, sid = _get_auth()
        url = f"{IMPACT_BASE_URL}/Mediapartners/{sid}/Ads/{ad_id}/TrackingLink"
        resp = requests.get(url, auth=auth)
        resp.raise_for_status()
        data = resp.json()
        return data.get("TrackingLink") or data.get("tracking_link")
    except Exception as e:
        print(f"[impact_client] get_tracking_link error: {e}")
        return None
