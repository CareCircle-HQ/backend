#!/bin/bash
# Environment switcher for CareCircle Django backend
# Usage: source scripts/switch_env.sh [local|dev|prod]

set -e

ENV=${1:-local}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

case "$ENV" in
  local)
    if [ -f "$PROJECT_DIR/.env" ]; then
      echo "✓ Using local environment (.env)"
      export ENVIRONMENT=local
    else
      echo "✗ .env file not found. Run from project root: cp .env.example .env"
      return 1
    fi
    ;;
  dev)
    if [ -f "$PROJECT_DIR/.env.dev" ]; then
      echo "✓ Using dev environment (.env.dev)"
      cp "$PROJECT_DIR/.env.dev" "$PROJECT_DIR/.env"
      export ENVIRONMENT=dev
    else
      echo "✗ .env.dev file not found"
      return 1
    fi
    ;;
  prod)
    if [ -f "$PROJECT_DIR/.env.prod" ]; then
      echo "⚠️  Using PRODUCTION environment (.env.prod)"
      echo "⚠️  Are you sure? This connects to the live database."
      read -p "Continue? (yes/no): " confirm
      if [ "$confirm" != "yes" ]; then
        echo "Cancelled."
        return 1
      fi
      cp "$PROJECT_DIR/.env.prod" "$PROJECT_DIR/.env"
      export ENVIRONMENT=production
      echo "✓ Production environment active"
    else
      echo "✗ .env.prod file not found"
      return 1
    fi
    ;;
  *)
    echo "Usage: source scripts/switch_env.sh [local|dev|prod]"
    echo ""
    echo "Environments:"
    echo "  local  - Local PostgreSQL (port 5433) or SQLite"
    echo "  dev    - AWS RDS Dev (carecircle db)"
    echo "  prod   - AWS RDS Production (Prod db) ⚠️"
    return 1
    ;;
esac

echo ""
echo "Database: $(grep DB_HOST "$PROJECT_DIR/.env" | cut -d= -f2)"
echo "Run: python manage.py runserver"
