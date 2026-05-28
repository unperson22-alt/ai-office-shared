"""Impact.com affiliate API client для AI Office."""
import os
import httpx

IMPACT_BASE_URL = "https://api.impact.com"



def get_campaigns() -> list[dict] | None:
    """Получить список доступных кампаний брендов."""
    try:
        sid = os.getenv("IMPACT_ACCOUNT_SID")
        auth_token = os.getenv("IMPACT_AUTH_TOKEN")
        if not sid or not auth_token:
            raise ValueError("credentials missing")
        auth = (sid, auth_token)
        url = f"{IMPACT_BASE_URL}/Mediapartners/{sid}/Campaigns"
        resp = httpx.get(url, auth=auth, params={"PageSize": 50})
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
        sid = os.getenv("IMPACT_ACCOUNT_SID")
        auth_token = os.getenv("IMPACT_AUTH_TOKEN")
        if not sid or not auth_token:
            raise ValueError("credentials missing")
        auth = (sid, auth_token)
        url = f"{IMPACT_BASE_URL}/Mediapartners/{sid}/Ads"
        params = {"PageSize": 50}
        if campaign_id:
            params["CampaignId"] = campaign_id
        if niche:
            params["Niche"] = niche
        resp = httpx.get(url, auth=auth, params=params)
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
        sid = os.getenv("IMPACT_ACCOUNT_SID")
        auth_token = os.getenv("IMPACT_AUTH_TOKEN")
        if not sid or not auth_token:
            raise ValueError("credentials missing")
        auth = (sid, auth_token)
        url = f"{IMPACT_BASE_URL}/Mediapartners/{sid}/Ads/{ad_id}/TrackingLink"
        resp = httpx.get(url, auth=auth)
        resp.raise_for_status()
        data = resp.json()
        return data.get("TrackingLink") or data.get("tracking_link")
    except Exception as e:
        print(f"[impact_client] get_tracking_link error: {e}")
        return None
