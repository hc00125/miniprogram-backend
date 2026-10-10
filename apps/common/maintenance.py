from django.conf import settings
from django.http import JsonResponse


class MaintenanceModeMiddleware:
    """维护模式中间件：MAINTENANCE_MODE=True 时所有 API 返回 503。

    豁免：
    - /api/health — 健康检查仍可访问，方便负载均衡器判断
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.MAINTENANCE_MODE:
            # 健康检查豁免
            if request.path == '/api/health':
                return self.get_response(request)

            return JsonResponse(
                {
                    'code': 503,
                    'message': '系统维护中，请稍后再试',
                    'detail': 'The system is under maintenance. Please try again later.',
                },
                status=503,
            )

        return self.get_response(request)