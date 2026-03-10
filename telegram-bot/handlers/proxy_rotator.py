import asyncio
import aiohttp
import random
import logging

logger = logging.getLogger(__name__)


class ProxyRotator:
    """Fetches free proxies and rotates them automatically"""
    
    def __init__(self):
        self.proxies = []
        self.current_index = 0
        self.failed_proxies = set()
    
    async def fetch_proxies(self):
        """Fetch free proxy list from multiple sources"""
        all_proxies = []
        
        sources = [
            self._fetch_from_proxylist,
            self._fetch_from_geonode,
            self._fetch_from_free_proxy_list,
        ]
        
        for source in sources:
            try:
                proxies = await source()
                all_proxies.extend(proxies)
            except Exception as e:
                logger.warning(f"Failed to fetch from source: {e}")
        
        # Remove duplicates and failed ones
        self.proxies = list(set(all_proxies) - self.failed_proxies)
        random.shuffle(self.proxies)
        self.current_index = 0
        
        logger.info(f"Loaded {len(self.proxies)} proxies")
        return len(self.proxies)
    
    async def _fetch_from_proxylist(self) -> list:
        """Fetch from free-proxy-list API"""
        proxies = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        for line in text.strip().split("\n"):
                            line = line.strip()
                            if ":" in line:
                                proxies.append(f"http://{line}")
        except Exception as e:
            logger.warning(f"proxyscrape fetch failed: {e}")
        return proxies
    
    async def _fetch_from_geonode(self) -> list:
        """Fetch from geonode free proxy API"""
        proxies = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://proxylist.geonode.com/api/proxy-list?limit=100&page=1&sort_by=lastChecked&sort_type=desc&protocols=http",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for p in data.get("data", []):
                            ip = p.get("ip")
                            port = p.get("port")
                            if ip and port:
                                proxies.append(f"http://{ip}:{port}")
        except Exception as e:
            logger.warning(f"geonode fetch failed: {e}")
        return proxies
    
    async def _fetch_from_free_proxy_list(self) -> list:
        """Fetch from free-proxy-list.net via API"""
        proxies = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=yes&anonymity=elite",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        for line in text.strip().split("\n"):
                            line = line.strip()
                            if ":" in line:
                                proxies.append(f"http://{line}")
        except Exception as e:
            logger.warning(f"free-proxy-list fetch failed: {e}")
        return proxies
    
    def get_next_proxy(self) -> str | None:
        """Get next working proxy"""
        if not self.proxies:
            return None
        
        attempts = 0
        while attempts < len(self.proxies):
            proxy = self.proxies[self.current_index % len(self.proxies)]
            self.current_index += 1
            
            if proxy not in self.failed_proxies:
                return proxy
            attempts += 1
        
        return None
    
    def mark_failed(self, proxy: str):
        """Mark a proxy as failed"""
        self.failed_proxies.add(proxy)
    
    def get_proxy_count(self) -> int:
        """Get number of available proxies"""
        return len(self.proxies) - len(self.failed_proxies)
    
    async def test_proxy(self, proxy: str) -> bool:
        """Test if a proxy is working"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://httpbin.org/ip",
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
    
    async def get_working_proxy(self, max_attempts: int = 10) -> str | None:
        """Get a tested working proxy"""
        for _ in range(max_attempts):
            proxy = self.get_next_proxy()
            if proxy is None:
                # Refresh proxy list
                await self.fetch_proxies()
                proxy = self.get_next_proxy()
                if proxy is None:
                    return None
            
            if await self.test_proxy(proxy):
                return proxy
            else:
                self.mark_failed(proxy)
        
        return None
