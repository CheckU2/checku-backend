# CheckU Backend

API REST para seguimiento de asistencia académica con Python, FastAPI y MongoDB Atlas. Las rutas de negocio se publican bajo `/api/v1`. Las fechas se guardan en UTC; la aplicación cliente debe mostrar la hora local usando la zona horaria detectada en cada dispositivo.



## Requisitos
- Python 3.11+ (recomendado 3.12/3.13).
- MongoDB Atlas o MongoDB compatible.
- Credenciales SMTP reales para verificación de correo y recuperación de contraseña.
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
Copia el resultado en `JWT_SECRET_KEY`. No uses la clave de ejemplo ni subas `.env` al repositorio. Para habilitar el registro en instituciones de prueba, configura `MULTI_INSTITUTION_REGISTRATION_ENABLED=true` en `.env`; el valor predeterminado es `false`.

## MongoDB Atlas
1. Crea un usuario de base de datos con privilegios mínimos para la base `checku`.
2. En Network Access permite solo la IP del servidor/desarrollador que necesita conectarse.
3. Copia la URI de Connect → Drivers y configúrala en `MONGODB_URI`.
4. Configura `MONGODB_DATABASE=checku`.
5. No guardes la URI en código, capturas, logs ni documentación.
6. En producción habilita TLS, copias de seguridad, alertas y pruebas periódicas de restauración.

## Correo SMTP
Configura `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` y `SMTP_USE_TLS`. `PASSWORD_RESET_BASE_URL` debe apuntar a la pantalla de restablecimiento del frontend. Para probar el envío, usa una cuenta de correo de desarrollo.

En Render Free, verifica un remitente en Brevo y guarda `BREVO_API_KEY`, `SMTP_FROM_EMAIL` (el remitente verificado) y `SMTP_FROM_NAME` en la sección **Environment** del servicio. Vuelve a desplegar el backend. Con `BREVO_API_KEY` configurada, CheckU envía los correos por HTTPS. Si la clave está vacía, usa SMTP.

### Cambio de contraseña con sesión iniciada

1. Envía `PATCH /api/v1/auth/change-password` con `current_password` y `new_password`. La API devuelve `202` al enviar el código al correo de la cuenta.
2. Envía `POST /api/v1/auth/change-password/confirm` con `{"code":"012345"}`. Ambas solicitudes requieren el token de acceso de la cuenta. El código vence en 10 minutos y admite cinco intentos.
3. Para pedir otro código, repite el primer paso después de 60 segundos. Al confirmar, el backend aplica la nueva contraseña y revoca las sesiones renovables.

### Verificación de correo

- `POST /api/v1/auth/register` responde `202`, guarda la solicitud temporal durante 24 horas y envía un código. La cuenta y sus asignaciones se crean al confirmar el código. Si el envío falla, la solicitud permite pedir un reenvío. Las solicitudes abandonadas se eliminan mediante el índice TTL de MongoDB.
- `POST /api/v1/auth/verify-email` recibe `{"email":"persona@example.com","code":"012345"}`. Envía el código como texto para conservar los ceros iniciales. Vence en 10 minutos, admite hasta cinco intentos y se consume al confirmar. Los códigos inválidos, vencidos o agotados devuelven `400 INVALID_VERIFICATION_CODE`.
- `POST /api/v1/auth/resend-verification` recibe `{"email":"persona@example.com"}`. Devuelve un mensaje genérico. Se puede generar otro código cada 60 segundos por cuenta, sujeto también al límite por IP. Al generar uno nuevo, el anterior queda invalidado.
- `POST /api/v1/auth/login` devuelve `403 EMAIL_NOT_VERIFIED` si la contraseña es correcta y el correo está pendiente. Las cuentas existentes y las creadas por administradores siguen su flujo habitual.

## Crear el superadministrador inicial
Configura temporalmente `BOOTSTRAP_ADMIN_EMAIL` y `BOOTSTRAP_ADMIN_PASSWORD` antes del primer arranque. Se crea como `super_admin` si el correo no existe. Usa una contraseña fuerte, protege `.env` y retira estas variables tras el bootstrap. No existe registro público de superadministradores. El superadministrador pertenece a la plataforma CheckU; no es una cuenta institucional.

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
- Configuración mediante Pydantic Settings y CORS explícito; las fechas se almacenan en UTC y cada dispositivo determina su hora local.
- Conexión MongoDB con pool/timeouts e índices para usuarios, sesiones renovables, tokens de recuperación, desafíos QR y asistencia.
- Hash Argon2, JWT de acceso de 10 minutos, refresh token rotatorio almacenado como hash, cierre de sesión y revocación de sesiones renovables.
- Registro público de alumnos y solicitudes de profesor en instituciones habilitadas; login con selección de contexto, perfil actual, cambio y recuperación de contraseña mediante SMTP.
- Contextos y permisos derivados de membresías/asignaciones activas, validados de nuevo en cada petición; la revocación en MongoDB invalida el acceso institucional inmediatamente.
- Alta de instituciones activas por plataforma, asignación de propietario institucional personal, revisión de solicitudes docentes, asignación de roles por institución/carrera/grupo y auditoría de estas acciones.
- CRUD genérico con lista blanca de campos y baja lógica para carreras, periodos, grupos, materias, perfiles académicos, inscripciones, asignaciones, horarios e incidencias.
- Sesiones persistentes, activación/cierre, QR y código temporal, registro de asistencia y corrección manual auditada.
- Justificantes y mensajes con estados e historial.
- Eventos de proximidad conservadores que quedan pendientes de revisión humana.
- Reportes básicos, auditoría y avatares privados con validación de imagen y re-encode para eliminar metadatos.

## Rutas principales
- `/api/v1/auth`: register, verify-email, resend-verification, login, refresh, logout, logout-all, me, forgot-password, reset-password, change-password, change-password/confirm.
- `/api/v1/institutions`: catálogo público, alta por plataforma, propietario, miembros, alcances, solicitudes de rol, asignación y configuración institucional.
- `/api/v1/users`: CRUD limitado, estado, baja lógica y avatar propio.
- `/api/v1/careers`, `/academic-periods`, `/groups`, `/subjects`, `/students`, `/teachers`, `/tutors`, `/enrollments`, `/assignments`, `/schedules`, `/incidents`.
- `/api/v1/sessions`: sesiones, activas, start/end, QR rotate/revoke.
- `/api/v1/attendance`: check-in QR/código, consultas y corrección manual.
- `/api/v1/justifications`: solicitudes, resolución y mensajes.
- `/api/v1/proximity`: activar, eventos y revisión.
- `/api/v1/reports`, `/api/v1/audit-logs`.

## Roles y plan multiinstitucional

| Rol | Alcance previsto |
| --- | --- |
| `platform_super_admin` | Crea instituciones y asigna al propietario institucional; no pertenece a una institución. |
| `institution_owner` | Máxima autoridad de una institución; administra miembros, aprueba personal y asigna roles institucionales. |
| `institution_admin` | Apoya la gestión de miembros y roles dentro de su institución. |
| `career_director` | Gestiona personal y actividades limitadas a su carrera. |
| `teacher` | Opera clases y sesiones que tenga asignadas. |
| `tutor` | Atiende grupos/alumnos asignados y sus incidencias correspondientes. |
| `student` | Consulta su información, clases y métricas personales. |

El login selecciona un contexto cuando la cuenta tiene varios roles o instituciones. La autorización del backend se deriva de asignaciones activas, no del rol enviado por el navegador. La aprobación y revocación de roles se auditan; la revocación invalida el contexto en la siguiente petición.

Para completar la operación académica multiinstitucional, el trabajo pendiente es: migrar/etiquetar carreras, grupos, clases, sesiones, asistencia, justificantes y reportes con `institution_id`; aplicar filtros tenant a todas sus lecturas/escrituras; definir permisos específicos por alcance; y probar con dos instituciones que no hay acceso cruzado. Mientras tanto, el guard bloquea las rutas institucionales no auditadas.

## Límites y pendientes antes de producción
- **Alcance multiinstitucional:** funcionan login/contexto, gestión institucional, aprobación de solicitudes docentes y rutas acotadas para que alumnos consulten sus datos y docentes trabajen con sus clases, sesiones y justificantes asignados. Las demás rutas académicas siguen rechazando cuentas institucionales hasta incorporar y probar filtros por institución; no se debe quitar el guard global para habilitarlas.
- **Integración externa:** Atlas y SMTP no se probaron aquí; configura credenciales y realiza pruebas de integración.
- **Política académica:** la fórmula del 80 % está como indicador provisional; la institución debe definir retardos, justificantes y sesiones canceladas. La penalización por tres abandonos requiere aprobación formal y aún no se aplica automáticamente.
- **Proximidad:** los eventos no sancionan automáticamente y requieren revisión humana. La aplicación móvil debe implementar desafíos/frescura y evidencia consistente.
- **Permisos académicos:** los routers genéricos aplican roles y filtro de carrera básico, pero las relaciones complejas de tutor, docente, grupo, materia y periodo deben verificarse con pruebas de integración antes de cargar datos reales. Se recomienda revisión de seguridad independiente.
- **Concurrencia/carga:** índices únicos e idempotencia básica están implementados en flujos principales; no se ha ejecutado benchmark de cientos/miles de solicitudes ni prueba de failover.
- **Rate limiting:** login y recuperación usan límites por proceso; para varias instancias debe usarse Redis o un limitador distribuido detrás de un proxy/WAF.
- **DDoS/producción:** FastAPI no sustituye WAF, proxy inverso, TLS, firewall, límites de red y alertas.
- **Auditoría:** conservar logs según política institucional, evitando datos sensibles y estableciendo retención.
- **Archivos:** el avatar usa almacenamiento privado local. En despliegue con varias instancias, mover a object storage privado y URLs firmadas.

### Alta de docentes y tutores

En el flujo institucional nuevo, un profesor se registra como solicitud pendiente y el propietario/administrador institucional aprueba o rechaza su acceso desde el panel institucional. Un tutor puede recibir su rol mediante una asignación explícita de la institución. La asignación crea o actualiza el perfil asociado y registra auditoría.

El endpoint legado `POST /api/v1/users` conserva compatibilidad con el modelo anterior. No lo uses como sustituto del flujo de aprobación institucional.

### Clases con código de inscripción del sistema legado

- Las cuentas sin institución conservan las rutas y permisos de clases previos.
- En cuentas institucionales, `GET /api/v1/classes`, `POST /api/v1/classes/join`, `GET /api/v1/classes/{class_id}/community` y `GET /api/v1/reports/dashboard` están limitados al alumno y validan su institución/inscripción.
- El resto de los endpoints académicos institucionales permanece bloqueado hasta completar el aislamiento multiinstitucional.
- Para el sistema legado, `POST /api/v1/classes` genera `join_code` de cinco caracteres alfanuméricos y `POST /api/v1/classes/join` acepta `{ "code": "K7M2P" }`.
- `PATCH /api/v1/classes/{class_id}`: gestores autorizados; permite `enrollment_open`, `is_active`, `teacher_id` y `regenerate_code`.
- `GET /api/v1/classes/{class_id}/students`: lista de inscritos visible para gestores y docente asignado.

El código de clase no es el código temporal de asistencia. Los `ObjectId` siguen siendo referencias internas. Para habilitar operaciones académicas institucionales faltan filtros por institución en sus consultas y escrituras y pruebas de aislamiento entre dos instituciones.
