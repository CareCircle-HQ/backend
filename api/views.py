from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RegisterSerializer, UserSerializer


class RegisterView(generics.CreateAPIView):
    """Public endpoint to create a new user."""

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class MeView(APIView):
    """Return the currently authenticated user (requires a valid JWT)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class HealthView(APIView):
    """Simple public health check."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({"status": "ok"})
