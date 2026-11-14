# Migraciones

Las tablas se crean automáticamente con `Base.metadata.create_all()` desde
`db/models.py:init_db()` (suficiente para desarrollo y paper trading).

Para gestión de esquema en producción usa Alembic (ya está en
`requirements.txt`):

```bash
alembic init db/migrations        # solo la primera vez
# editar db/migrations/env.py para apuntar target_metadata = Base.metadata
alembic revision --autogenerate -m "init"
alembic upgrade head
```
