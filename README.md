# CheckU Backend

API REST para seguimiento de asistencia académica con Python, FastAPI y MongoDB Atlas. Las rutas de negocio se publican bajo `/api/v1`. Las fechas se guardan en UTC; la zona horaria institucional predeterminada es `America/Mexico_City`.



## Requisitos
- Python 3.11+ (recomendado 3.12/3.13).
- MongoDB Atlas o MongoDB compatible.
- Credenciales SMTP reales para recuperación por correo.
- PowerShell en Windows.

## Instalación (PowerShell)
```powershell
cd D:\ruta\checku-backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Genera una clave de firma:
```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
Copia el resultado en `JWT_SECRET_KEY`. No uses la clave de ejemplo ni subas `.env` al repositorio.

## MongoDB Atlas
1. Crea un usuario de base de datos con privilegios mínimos para la base `checku`.
2. En Network Access permite solo la IP del servidor/desarrollador que necesita conectarse.
3. Copia la URI de Connect → Drivers y configúrala en `MONGODB_URI`.
4. Configura `MONGODB_DATABASE=checku`.
5. No guardes la URI en código, capturas, logs ni documentación.
6. En producción habilita TLS, copias de seguridad, alertas y pruebas periódicas de restauración.

## Correo SMTP
Configura `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` y `SMTP_USE_TLS`. `PASSWORD_RESET_BASE_URL` debe apuntar a la pantalla de restablecimiento del frontend. La API no simula el envío: si SMTP no está configurado, devuelve un error genérico de servicio y no revela el token. Prueba primero con una cuenta de correo de desarrollo.

## Crear el superadministrador inicial
Configura temporalmente `BOOTSTRAP_ADMIN_EMAIL` y `BOOTSTRAP_ADMIN_PASSWORD` antes del primer arranque. Se crea como `super_admin` si el correo no existe. Usa una contraseña fuerte, protege `.env` y retira estas variables tras el bootstrap. No existe registro público de superadministradores. El registro público habilitado solo crea rol `student`.

## Ejecutar
```powershell
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
- API: `http://127.0.0.1:8000`
- OpenAPI: `http://127.0.0.1:8000/docs` (no exponer públicamente en producción sin política explícita).
- Salud: `GET /api/v1/health`
- Preparación con comprobación de MongoDB: `GET /api/v1/ready`

## Pruebas
```powershell
python -m pytest -q
```
La prueba de `/ready` debe devolver 503 si Atlas no está configurado o no responde. Las pruebas de integración requieren una base de pruebas independiente; nunca apuntes pruebas destructivas a datos productivos.

## Funcionalidades implementadas
- Configuración mediante Pydantic Settings, CORS explícito y zona horaria institucional.
- Conexión MongoDB con pool/timeouts e índices para usuarios, sesiones renovables, tokens de recuperación, desafíos QR y asistencia.
- Hash Argon2, JWT de acceso de 10 minutos, refresh token rotatorio almacenado como hash, cierre de sesión y revocación de sesiones renovables.
- Registro público solo de estudiantes; login; perfil actual; cambio y recuperación de contraseña mediante SMTP.
- CRUD genérico con lista blanca de campos y baja lógica para carreras, periodos, grupos, materias, perfiles académicos, inscripciones, asignaciones, horarios e incidencias.
- Sesiones persistentes, activación/cierre, QR y código temporal, registro de asistencia y corrección manual auditada.
- Justificantes y mensajes con estados e historial.
- Eventos de proximidad conservadores que quedan pendientes de revisión humana.
- Reportes básicos, auditoría y avatares privados con validación de imagen y re-encode para eliminar metadatos.

## Rutas principales
- `/api/v1/auth`: register, login, refresh, logout, logout-all, me, forgot-password, reset-password, change-password.
- `/api/v1/users`: CRUD limitado, estado, baja lógica y avatar propio.
- `/api/v1/careers`, `/academic-periods`, `/groups`, `/subjects`, `/students`, `/teachers`, `/tutors`, `/enrollments`, `/assignments`, `/schedules`, `/incidents`.
- `/api/v1/sessions`: sesiones, activas, start/end, QR rotate/revoke.
- `/api/v1/attendance`: check-in QR/código, consultas y corrección manual.
- `/api/v1/justifications`: solicitudes, resolución y mensajes.
- `/api/v1/proximity`: activar, eventos y revisión.
- `/api/v1/reports`, `/api/v1/audit-logs`.

## Límites y pendientes antes de producción
- **Verificación de dependencias y pruebas:** no fue posible instalar dependencias ni ejecutar pytest en este entorno sin red. Ejecuta todo en un entorno local con `requirements.txt`.
- **Integración externa:** Atlas y SMTP no se probaron aquí; configura credenciales y realiza pruebas de integración.
- **Política académica:** la fórmula del 80 % está como indicador provisional; la institución debe definir retardos, justificantes y sesiones canceladas. La penalización por tres abandonos requiere aprobación formal y aún no se aplica automáticamente.
- **Proximidad:** los eventos no sancionan automáticamente y requieren revisión humana. La aplicación móvil debe implementar desafíos/frescura y evidencia consistente.
- **Permisos académicos:** los routers genéricos aplican roles y filtro de carrera básico, pero las relaciones complejas de tutor, docente, grupo, materia y periodo deben verificarse con pruebas de integración antes de cargar datos reales. Se recomienda revisión de seguridad independiente.
- **Concurrencia/carga:** índices únicos e idempotencia básica están implementados en flujos principales; no se ha ejecutado benchmark de cientos/miles de solicitudes ni prueba de failover.
- **Rate limiting:** login y recuperación usan límites por proceso; para varias instancias debe usarse Redis o un limitador distribuido detrás de un proxy/WAF.
- **DDoS/producción:** FastAPI no sustituye WAF, proxy inverso, TLS, firewall, límites de red y alertas.
- **Auditoría:** conservar logs según política institucional, evitando datos sensibles y estableciendo retención.
- **Archivos:** el avatar usa almacenamiento privado local. En despliegue con varias instancias, mover a object storage privado y URLs firmadas.

### Alta de docentes y tutores desde Usuarios

`POST /api/v1/users` acepta `employee_id` (obligatorio cuando `role` es `teacher` o `tutor`), `career_id` (opcional) y `department` (opcional para docentes). Al crear una cuenta docente o tutor se crea su perfil correspondiente en `teachers` o `tutors`. Para usuarios ya existentes se puede crear el perfil desde los endpoints académicos seleccionando la cuenta en la interfaz web.

## Clases con código de inscripción (actualización)

- `POST /api/v1/classes`: `admin`, `super_admin`, `career_admin` y `tutor` autorizados. Body: `career_id` (solo admins generales), `subject_id`, `group_id`, `period_id`, `teacher_id` (ID interno del **perfil docente**, elegido mediante selector web). Genera `join_code` de cinco caracteres alfanuméricos.
- `GET /api/v1/classes`: clases visibles por rol. El docente solo ve las suyas, el alumno las que ha unido y el administrador de carrera/tutor las de su carrera.
- `POST /api/v1/classes/join`: solo alumnos, body `{ "code": "K7M2P" }`. La inscripción es idempotente en `class_memberships`.
- `PATCH /api/v1/classes/{class_id}`: gestores autorizados; permite `enrollment_open`, `is_active`, `teacher_id` y `regenerate_code`.
- `GET /api/v1/classes/{class_id}/students`: lista de inscritos visible para gestores y docente asignado.

El código de clase no es el código temporal de asistencia. Los `ObjectId` siguen siendo referencias internas y no se solicitan manualmente en la nueva pantalla de clases. Para limitar el alcance, el backend valida carrera y rol en las operaciones.
