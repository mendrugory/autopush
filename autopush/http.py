"""HTTP Server Protocol Factories on top of cyclone"""
from typing import (  # noqa
    Any,
    Callable,
    Dict,
    Optional,
    Sequence,
    Tuple,
    Type
)

import cyclone.web
from twisted.internet import reactor
from twisted.web.client import (
    _HTTP11ClientFactory,
    Agent,
    HTTPConnectionPool,
)

from autopush.base import BaseHandler
from autopush.db import DatabaseManager
from autopush.router import routers_from_settings
from autopush.router.interface import IRouter  # noqa
from autopush.settings import AutopushSettings  # noqa
from autopush.ssl import AutopushSSLContextFactory
from autopush.web.health import (
    HealthHandler,
    MemUsageHandler,
    StatusHandler
)
from autopush.web.limitedhttpconnection import LimitedHTTPConnection
from autopush.web.log_check import LogCheckHandler
from autopush.web.message import MessageHandler
from autopush.web.registration import (
    ChannelRegistrationHandler,
    NewRegistrationHandler,
    SubRegistrationHandler,
    UaidRegistrationHandler,
)
from autopush.web.simplepush import SimplePushHandler
from autopush.web.webpush import WebPushHandler
from autopush.websocket import (
    NotificationHandler,
    PushServerProtocol,
    RouterHandler,
)
from autopush.websocket import PushServerProtocol  # noqa

APHandlers = Sequence[Tuple[str, Type[BaseHandler]]]
CycloneLogger = Callable[[BaseHandler], None]


def skip_request_logging(handler):
    # type: (cyclone.web.RequestHandler) -> None
    """Skip cyclone's request logging"""


class BaseHTTPFactory(cyclone.web.Application):

    ap_handlers = None  # type: APHandlers

    health_ap_handlers = (
        (r"^/status", StatusHandler),
        (r"^/health", HealthHandler),
    )

    def __init__(self,
                 ap_settings,    # type: AutopushSettings
                 db,             # type: DatabaseManager
                 handlers=None,  # type: APHandlers
                 log_function=skip_request_logging,  # type: CycloneLogger
                 **kwargs):
        # type: (...) -> None
        self.ap_settings = ap_settings
        self.db = db
        self.noisy = ap_settings.debug

        cyclone.web.Application.__init__(
            self,
            handlers=self.ap_handlers if handlers is None else handlers,
            default_host=self._hostname,
            debug=ap_settings.debug,
            log_function=log_function,
            **kwargs
        )

    def add_health_handlers(self):
        """Add the health check HTTP handlers"""
        self.add_handlers(".*$", self.health_ap_handlers)

    @property
    def _hostname(self):
        return self.ap_settings.hostname

    @classmethod
    def for_handler(cls,
                    handler_cls,    # Type[BaseHTTPFactory]
                    ap_settings,    # type: AutopushSettings
                    db=None,        # type: Optional[DatabaseManager]
                    **kwargs):
        # type: (...) -> BaseHTTPFactory
        """Create a cyclone app around a specific handler_cls for tests.

        Creates an uninitialized (no setup() called) DatabaseManager
        from settings if one isn't specified.

        handler_cls must be included in ap_handlers or a ValueError is
        thrown.

        """
        if 'handlers' in kwargs:  # pragma: nocover
            raise ValueError("handler_cls incompatibile with handlers kwarg")
        for pattern, handler in cls.ap_handlers + cls.health_ap_handlers:
            if handler is handler_cls:
                if db is None:
                    db = DatabaseManager.from_settings(ap_settings)
                return cls._for_handler(
                    ap_settings,
                    db=db,
                    handlers=[(pattern, handler)],
                    **kwargs
                )
        raise ValueError("{!r} not in ap_handlers".format(
            handler_cls))  # pragma: nocover

    @classmethod
    def _for_handler(cls, ap_settings, **kwargs):
        # type: (AutopushSettings, **Any) -> BaseHTTPFactory
        """Create an instance w/ default kwargs for for_handler"""
        raise NotImplementedError  # pragma: nocover


class EndpointHTTPFactory(BaseHTTPFactory):

    ap_handlers = (
        (r"/spush/(?:(?P<api_ver>v\d+)\/)?(?P<token>[^\/]+)",
         SimplePushHandler),
        (r"/wpush/(?:(?P<api_ver>v\d+)\/)?(?P<token>[^\/]+)",
         WebPushHandler),
        (r"/m/(?P<message_id>[^\/]+)", MessageHandler),
        (r"/v1/(?P<type>[^\/]+)/(?P<app_id>[^\/]+)/registration",
         NewRegistrationHandler),
        (r"/v1/(?P<type>[^\/]+)/(?P<app_id>[^\/]+)/registration/"
         r"(?P<uaid>[^\/]+)",
         UaidRegistrationHandler),
        (r"/v1/(?P<type>[^\/]+)/(?P<app_id>[^\/]+)/registration/"
         r"(?P<uaid>[^\/]+)/subscription",
         SubRegistrationHandler),
        (r"/v1/(?P<type>[^\/]+)/(?P<app_id>[^\/]+)/registration/"
         r"(?P<uaid>[^\/]+)/subscription/(?P<chid>[^\/]+)",
         ChannelRegistrationHandler),
        (r"/v1/err(?:/(?P<err_type>[^\/]+))?", LogCheckHandler),
    )

    protocol = LimitedHTTPConnection

    def __init__(self,
                 ap_settings,  # type: AutopushSettings
                 db,           # type: DatabaseManager
                 routers,      # type: Dict[str, IRouter]
                 **kwargs):
        # type: (...) -> None
        BaseHTTPFactory.__init__(self, ap_settings, db=db, **kwargs)
        self.routers = routers

    def ssl_cf(self):
        # type: () -> Optional[AutopushSSLContextFactory]
        """Build our SSL Factory (if configured).

        Configured from the ssl_key/cert/dh_param and client_cert
        values.

        """
        settings = self.ap_settings
        if not settings.ssl_key:
            return None
        return AutopushSSLContextFactory(
            settings.ssl_key,
            settings.ssl_cert,
            dh_file=settings.ssl_dh_param,
            require_peer_certs=settings.enable_tls_auth
        )

    @classmethod
    def _for_handler(cls, ap_settings, db, routers=None, **kwargs):
        if routers is None:
            routers = routers_from_settings(
                ap_settings,
                db=db,
                agent=agent_from_settings(ap_settings)
            )
        return cls(ap_settings, db=db, routers=routers, **kwargs)


class InternalRouterHTTPFactory(BaseHTTPFactory):

    ap_handlers = (
        (r"/push/([^\/]+)", RouterHandler),
        (r"/notif/([^\/]+)(?:/(\d+))?", NotificationHandler),
    )

    def __init__(self,
                 ap_settings,  # type: AutopushSettings
                 db,           # type: DatabaseManager
                 clients,      # type: Dict[str, PushServerProtocol]
                 **kwargs):
        # type: (...) -> None
        BaseHTTPFactory.__init__(self, ap_settings, db, **kwargs)
        self.clients = clients

    @property
    def _hostname(self):
        return self.ap_settings.router_hostname

    def ssl_cf(self):
        # type: () -> Optional[AutopushSSLContextFactory]
        """Build our SSL Factory (if configured).

        Configured from the router_ssl_key/cert and ssl_dh_param
        values.

        """
        settings = self.ap_settings
        if not settings.router_ssl_key:
            return None
        return AutopushSSLContextFactory(
            settings.router_ssl_key,
            settings.router_ssl_cert,
            dh_file=settings.ssl_dh_param
        )

    @classmethod
    def _for_handler(cls, ap_settings, db, clients=None, **kwargs):
        if clients is None:
            clients = {}
        return cls(ap_settings, db=db, clients=clients, **kwargs)


class MemUsageHTTPFactory(BaseHTTPFactory):

    ap_handlers = (
        (r"^/_memusage", MemUsageHandler),
    )


class QuietClientFactory(_HTTP11ClientFactory):
    """Silence the start/stop factory messages."""
    noisy = False


def agent_from_settings(settings):
    # type: (AutopushSettings) -> Agent
    """Create a twisted.web.client Agent from settings"""
    # Use a persistent connection pool for HTTP requests.
    pool = HTTPConnectionPool(reactor)
    if not settings.debug:
        pool._factory = QuietClientFactory
    return Agent(reactor, connectTimeout=settings.connect_timeout, pool=pool)
