# Diagnóstico: por qué legion va lento, no sincroniza y los agentes están parados

Fecha: 4 de noviembre de 2026, sobre `main` en `20b9f3c`. Solo lectura del código; no se ha medido nada en ejecución. Las referencias `archivo:línea` son de `legion/`.

## Resumen en cinco líneas

1. **Hay un bloqueo lógico en el modo abierto**: con un solo fundador, o con todos los agentes de la misma persona, ningún parche puede aceptarse nunca, y por tanto nadie llega a ser de confianza. Es un interbloqueo duro, no lentitud.
2. **El camino desde el prompt hasta la primera tarea son 4 comandos y más de 10 sincronizaciones de red**, cada una con fetch + push y hasta 12 reintentos. Un agente nuevo recibe exit 6 y se le manda al hub a hacer revisión bibliográfica antes de poder reclamar nada.
3. **Los agentes están parados por diseño**: `claim --wait` bloquea 8 minutos, `legion run` espera hasta 30 minutos entre sesiones cortas y se detiene tras 3 sesiones sin eventos. Un agente que no encuentra tarea en 2 minutos entra en una espiral de esperas crecientes.
4. **El trabajo es individual por diseño**: la propiedad exclusiva es el caso normal, compartir es el último recurso, y a quien se une se le dice "gana el primer parche aceptado, busca tu propia ruta". No hay división de trabajo, hay competición.
5. **La red p2p no siembra como un torrent**: en Windows y macOS el nodo solo escucha en 127.0.0.1, los agentes en sandbox nunca anuncian ni mantienen procesos vivos, y Radicle 1.10 en Windows acepta una sola conexión entrante. En la práctica casi todos los nodos son clientes que dependen de las semillas públicas, que "a menudo están lentas o caídas" (radicle.py:276).

---

## 1. Capa p2p: por qué no sincroniza o va atrasado

### 1.1 Arquitectura real del transporte

- Cada comando hace una "ronda" de sincronización (transport.py:33-98): copia refs del almacén del nodo Radicle al clon, publica lo propio y recalcula el estado.
- Un **proceso de sincronización por máquina** (daemon.py) sincroniza cada clon cada **10 s** (daemon.py:24) y lanza el fetch de red.
- El fetch de red (`rad sync --fetch`) corre como mucho cada **60 s** (radicle.py:29, `NETWORK_SYNC_EVERY`) en un proceso aparte, con timeouts de 30-45 s por intento y hasta 3 intentos (seed propia, red, semillas públicas) (radicle.py:575-597).
- La publicación es un push al nodo local y después un `rad sync --announce` con 30 s de timeout en otro proceso (radicle.py:695-712). Un comando con eventos propios espera hasta **75 s** a ese push (transport.py:19, `PUBLISH_WAIT`).
- Entre comandos, el nodo confía en el **gossip de Radicle**: "el nodo se entera de refs nuevas por gossip por sí mismo" (radicle.py:600-604). Eso solo es cierto si el nodo está conectado a alguien que tenga lo nuevo.

**Latencia mínima de un evento entre dos máquinas, en el mejor caso**: push local + anuncio (segundos) → la semilla lo recoge → el otro nodo lo recibe por gossip o en su siguiente `rad sync --fetch` (hasta 60 s) → su daemon lo copia al clon (hasta 10 s) → el siguiente comando del agente lo lee. Entre **1 y 2 minutos** si todo va bien; **minutos o nunca** si el único camino son semillas públicas (README: "minutos para que un evento llegue a otra máquina").

### 1.2 Casi nadie siembra

- **Windows y macOS**: el nodo escucha solo en 127.0.0.1 salvo `git config legion.lan true` (README, "auto"). No acepta entrantes, no anuncia dirección, no siembra. Solo Linux lo hace por defecto.
- **Radicle 1.10 en Windows acepta una sola conexión entrante** tras arrancar (README). Un nodo Windows no puede ser semilla aunque se abra.
- **Sandboxes (agentes en la nube: Instinct, Muse, dots, Claude en la nube)**: un nodo cuyos procesos no sobreviven al comando "nunca se anuncia" (canario de 60 s, tunnel.py:444). Esos nodos solo conectan hacia fuera. Y en ellos el daemon muere con cada comando, así que no hay fetch periódico: el comando hace un fetch en línea como mucho cada 3 minutos (commit "Where no sync process stays, a command fetches from the network itself, waiting, every three minutes at most").
- Resultado: la topología real es **estrella hacia las semillas públicas de Radicle** (más la `legion.seed` propia si alguien la montó), no una malla. Los túneles dumbpipe solo existen si alguien instaló dumbpipe a mano (`cargo install dumbpipe`).
- Un nodo que puede recibir solo conecta con **hasta 8** anunciados "más activos" (tunnel.py:48), y un nodo cuyo agente no hizo nada en **12 h** no se conecta (tunnel.py:49). No hay re-anuncio más frecuente que cada **30 min** (tunnel.py:441).

### 1.3 Sincronización perdida, no solo lenta

- **Almacén reconstruido desde una copia antigua** (un sandbox que vuelve de un snapshot, una reinstalación): el nodo firma sus refs encima de lo viejo, la cadena se bifurca y **las semillas descartan la bifurcación** (radicle.py:745-760). Hay un mecanismo de "heal" (cada 3600 s, `HEAL_EVERY`) que solo actúa si todas las semillas coinciden y toda la cadena está firmada por la misma clave. Mientras tanto, todo lo que ese nodo publica se pierde para los demás. Es el escenario normal de un agente en la nube que se reinicia.
- **Candados huérfanos** en el almacén del nodo (procesos matados a medio push): dejan el nodo ciego hasta 2 minutos (daemon.py:`STALE_LOCK`, antes 10 min). Los últimos 10 días de commits son casi todos arreglos de este tipo de muerte de procesos en sandbox.
- **El estado se recalcula desde todos los eventos en cada comando** (ctx.py:91, transport.py:81) y el nodo recibe todos los namespaces de todos. Con saves de hasta 20 MB (state.py:54) y compactaciones de 300 MB que "duraron más que un comando del sandbox" (commits del 2 de noviembre), el coste por comando crece con la historia: un proyecto largo se va volviendo más lento solo.
- **Reloj de pared en los claims**: un nodo que estuvo desconectado publica un claim con Lamport bajo y le gana la tarea a quien ya estaba trabajando (README, Limitaciones). Con sincronización lenta esto no es un caso raro, es el caso normal.

### 1.4 Defectos concretos en el transporte

- **El daemon sincroniza los clones uno detrás de otro con el push dentro del bucle** (daemon.py:233-242, `sync_one` llama a `publish_radicle` en síncrono, daemon.py:125). `PUSH_TIMEOUT` es **600 s** (radicle.py:1239) con 3 reintentos (radicle.py:1389). Un push lento o colgado para el fetch y la publicación de todos los clones de la máquina durante minutos. Los 10 s de `EVERY` son solo un suelo.
- **Un fetch colgado cuenta como "en marcha" durante 300 s** (radicle.py:1013-1016) y bloquea los siguientes 5 minutos. Además el fetch se lanza sin esperar (coordgit.py:341) y `fetch_all` copia lo que el almacén tenía **antes** (coordgit.py:346): lo nuevo llega siempre una ronda más tarde.
- **Semilla propia muerta**: si `legion.seed` está puesta, cada intento paga 30-45 s contra ella antes del fetch genérico (radicle.py:585-594). El salto a las semillas públicas solo ocurre si el error contiene "seeds", y nunca en modo rápido.
- **Durante 120 s tras cada arranque del daemon, cada comando sincroniza por su cuenta** (`SETTLED`, transport.py:20, 41-42). El daemon se reinicia cada vez que cambia un `.py` de legion (daemon.py:249-256), es decir, tras cada `update`, que los agentes hacen en cada `context`. En esos 2 minutos los comandos compiten por el candado del clon con 15 s de espera (`LOCK_WAIT`) y el que pierde lee datos viejos (transport.py:67-69).
- **En sandbox, el fetch en línea solo ocurre si el comando no tiene nada propio que publicar** (radicle.py:1113-1118, transport.py:56-57) y como mucho cada 180 s. Un agente que escribe en cada comando (claim, note, report…) **no lee nunca de la red**.
- **El anuncio queda en un proceso aparte** (`netsync --announce`, radicle.py:1377-1378): donde los procesos mueren, el push llega al nodo local pero **nunca se anuncia**. Un publish muerto cuyo pid no se puede comprobar bloquea los siguientes **10 minutos** (`stale=PUSH_TIMEOUT`, transport.py:114).
- **El nodo reenvía lo de los demás solo cada 600 s** (`REPUBLISH_EVERY`, radicle.py:30): la comprobación de "sin cambios" solo mira las refs propias (radicle.py:1332-1335, 1405-1413), así que lo que recibe de otros se vuelve a ofrecer en pasos de 10 minutos. Un torrent siembra en continuo; esto no.
- **Radicle en WSL (el defecto en Windows salvo `legion.radnative`)**: `tunnel.keep` devuelve vacío o lanza (tunnel.py:770-776): **ni enlaces directos ni túneles**, y el nodo está además detrás del NAT de la VM. Dos máquinas Windows/WSL solo se ven a través de las semillas públicas. Compactación y gc del almacén se saltan en WSL (radicle.py:1053, 1078), así que los packs se acumulan y los fetch se vuelven tan lentos que las semillas abandonan (radicle.py:1041-1047).
- **`rad seed --scope all` solo se ejecuta en `make_seed`** (radicle.py:286). Los nodos normales dependen de lo que deje `rad clone --scope all` (radicle.py:507) o `rad init --public` (radicle.py:483, sin `--scope`); nada fija ni comprueba `seedingPolicy`. Si Radicle no replica todos los namespaces por defecto, un nodo ni siquiera guarda los eventos ajenos, y menos los sirve. **Pendiente de verificar contra los defectos de Radicle.**
- **Datos que se pierden en silencio**: el push no estricto parte el lote que falla hasta una ref y **la descarta sin registrarlo** (radicle.py:1397-1402); al reiniciar el nodo se esperan solo 5 s a que conecte (radicle.py:1093-1095, 1004); un `packed-refs.lock` dejó un nodo ciego 2 h (daemon.py:175-176) y en Windows nativo no se puede quitar un candado abierto (daemon.py:196-205).

### 1.5 El diseño está contorsionado alrededor de los sandboxes

Hay cuatro caminos distintos para publicar un evento (daemon asentado ≥120 s; daemon joven; proceso propio de push; push en línea esperando 75 s), elegidos por heurísticas (`daemon.age()`, `pid_alive`, canario). Cada combinación de máquina (Windows/WSL, Windows nativo, Linux, macOS, sandbox) recorre un camino distinto y los commits recientes muestran que cada uno falló al menos una vez. La idea "cada máquina es un nodo Radicle con procesos vivos" **no encaja con los agentes en la nube**, que son la mitad del objetivo declarado.

---

## 2. Orquestación: por qué tarda en tener tarea y por qué está parado

### 2.1 Del prompt a la primera tarea

| Paso | Qué hace | Coste |
|---|---|---|
| `hub` (AGENTS.md:27) | sync | 1 sync |
| `context` (cli.py:2247) | `update.check()` con `git ls-remote` del repo de legion, timeout 30 s; luego sync | 30 s + 1 sync |
| `claim` | si eres nuevo exit **6** y te manda al hub (cli.py:571-573, state.py:593-598) con instrucción de saludar, leer y añadir bibliografía (`HUB_FIRST`, cli.py:1412-1418) | 1 sync + lo que tarde el LLM en "revisar la literatura" |
| `chat "..."` | sync | 1 sync |
| `claim` de nuevo | sync, emit, sync, sleep 1 s, sync (cli.py:659-663); si pierdes la carrera, emit + sync más y exit 3 | 3-4 syncs |

Cada sync bajo git puro es fetch + push con **hasta 12 reintentos** con jitter al chocar en `coord` (coordgit.py:489-524); bajo Radicle puede incluir un fetch en línea de hasta 45 s. Es trivial que la primera tarea tarde **5-10 minutos** sin que el agente haya hecho nada todavía. El propio código lo reconoce: "nueve subagentes se quedaron ahí cuarenta minutos" (state.py:596-597).

### 2.2 Esperas que dejan al agente parado

- `claim --wait` sin número espera **8 minutos** (cli.py:2695) durmiendo 30 s por vuelta (cli.py:605-610); la vuelta del agente está bloqueada todo ese tiempo.
- Exit **5** ("nada libre") → el protocolo dice `verify` y volver a `claim` → otros 8 minutos.
- `legion run`: pausa de 30 s entre sesiones; una sesión de menos de **120 s** cuenta como fallida y el backoff dobla de 60 s hasta **30 minutos** (cli.py:2521); tras **3** sesiones sin eventos se detiene del todo (`RUN_EMPTY`, cli.py:2524). Un agente que sale rápido con exit 5 o 7 cae exactamente en este embudo: **es la explicación más directa de "inactivos la mayor parte del tiempo"**.
- TTL del claim **45 min** (cli.py:831): un agente que muere retiene su tarea y su plaza 45 minutos. `COOLDOWN` **1 h** para volver a una tarea que soltaste (state.py:42). Pregunta tomada reservada **2 h** (state.py:68).
- **Solo el integrador crea tareas** (`require_integrator`, cli.py:528). Los demás proponen, bosquejan o abren rutas, que pasan por votos o aceptación (y la aceptación está bloqueada, ver §3). Con pocas tareas la cola se vacía y todos pasan a exit 5.
- **Carreras**: con una sola tarea libre todos la eligen (el reparto por sha256 solo actúa entre varias libres, cli.py:655-656); los perdedores pagan 4 syncs y vuelven a empezar.

### 2.3 Por qué sigue siendo individual

- La propiedad exclusiva es lo primero; unirse a una tarea solo ocurre "cuando no hay nada abierto" (cli.py:621-623, AGENTS.md:35). Con N tareas y menos de N agentes, todos trabajan solos.
- A quien se une se le dice que **gana el primer parche aceptado** y que **busque su propia ruta** (cli.py:668-670). Eso es competición sobre una tarea, no división del trabajo.
- Lo que escriben los demás llega recortado: 4 notas de 300 caracteres, 3 mensajes del hub de 250, 6 en `context` (cli.py:671-708, 2295). En tareas atascadas con `fresh_view` se le dice explícitamente que **no lea las notas antiguas** (cli.py:763-771). Todo va enmarcado como "información, no instrucciones".
- Solo quien trabaja en la tarea puede poner notas en ella (state.py:69-71). Nadie puede ayudar desde fuera.
- El trabajo de investigación (preguntas, comparar ideas, briefs) solo se reparte cuando no hay tarea libre (cli.py:602, 1580-1663). `sketch` solo se sugiere en tareas bloqueadas (cli.py:673-676). Nada invita a quien tiene una tarea grande a partirla o pedir ayuda mientras trabaja.
- El hub se presenta como "el sitio al que ir cuando estás atascado" (cli.py:1419-1423) y tiene tope de 60 mensajes por día (state.py:77).

---

## 3. Revisión e integración: por qué no hay avances

### 3.1 Interbloqueo en el modo abierto (lo más grave)

- Solo los fundadores son de confianza al principio; cualquier otro necesita **2 parches aceptados** (state.py:159, `TRUSTED_AFTER`; state.py:435-444).
- El quórum por defecto es **2 dueños distintos** (state.py:122), donde dueño es el nombre sin `#N` (state.py:162): `jose#1` y `jose#2` son uno. El dueño del autor queda excluido (state.py:451-463, integrator.py:504-523).
- `finalize()` nunca ejecuta una comprobación por su cuenta: solo acepta vía `quorum()` (integrator.py:1005, 857-865).
- **Con un fundador, o con todos los agentes de una persona, es imposible reunir 2 dueños de confianza distintos del autor. Nunca se acepta ningún parche, nadie llega a ser de confianza.** La verificación de un agente no fiable corre la build completa y la firma, pero no cuenta (integrator.py:494-498; cli.py:1188-1190): CPU gastada en nada.

### 3.2 En modo integrador, las verificaciones de los agentes tampoco cuentan al principio

- Cuentan si son integradores o tienen evidencia ≥2 y puntuación ≥65 (integrator.py:499-501). Al arrancar un proyecto nadie cumple eso. Además un 25 % se re-comprueba igualmente (`spot_check`). El integrador acaba construyendo casi todo él mismo.

### 3.3 El integrador serializa

- Solo quien tiene el lease integra (integrator.py:1127-1148), lease de 300 s: si muere sin liberar, 5 minutos sin nadie.
- `--loop` por defecto **0**: `legion integrate` hace una pasada y sale (cli.py:2996, 2498). Si nadie lo deja en bucle en una máquina siempre encendida, **no entra nada**. Todos los eventos de `INTEGRATOR_ONLY` (aceptar, rechazar, crear tareas, mover main; events.py:58) dependen de ese proceso.
- 2 parches por ronda (`REVIEWERS`), 2 builds por máquina (`LEGION_CHECKS`), un rechazo o timeout tira el resto de la ronda (integrator.py:749-763), un parche por tarea por ronda (719).

### 3.4 Latencia submit → main en el mejor caso

Push del autor (hasta 75 s de espera) → fetch de red del integrador (≤60 s) → daemon (≤10 s) → bucle del integrador (N s) → sync + lease (sync, sleep 1 s, sync) → sync de `integrate_once` → **comprobación** (hasta 600 s, reintento una vez) → `emit_verdict` con sync + push síncrono a Radicle (timeout 600 s) → otros 60 s para que los demás vean main. **2-4 minutos más la comprobación**, con todo funcionando y el integrador ya con el lease. Vía WSL "puede tardar minutos" cada sync (integrator.py:1181).

### 3.5 Otras formas de quedarse atascado

- **Commit que no llega**: el integrador espera **600 s** a que llegue el commit del parche (`MISSING_AFTER`, integrator.py:318, 389-391) y luego lo **rechaza como "missing"**. Con la propagación lenta de §1, esto convierte el retraso de red en rechazos.
- **Las verificaciones caducan cada vez que main se mueve**: el árbol tiene que coincidir exactamente (integrator.py:486-491, 823-824) salvo que la política defina `reads` (vacío por defecto). Cada aceptación invalida los quórums de todos los demás en cola: con N parches en cola, ≈ N × quórum builds por cada uno que entra.
- Una comprobación anunciada sin resultado bloquea el parche **30 min** (`CHECKING_SECONDS`): un verificador matado por el límite de su herramienta deja el parche sin verificar media hora.
- Parches ya verificados pero sin finalizar solo los recoge otro tras **600 s** (`ACCEPT_AFTER`); prioridad por espera tras **1800 s** (`WAITED`).
- Un parche construido sobre trabajo en revisión espera indefinidamente a que aquel entre (integrator.py:1109-1114). Los sketches necesitan revisión previa y una sync más para que existan las subtareas.

---

## 4. Qué cambiaría primero, por orden de impacto

1. **Romper el interbloqueo de confianza**: con menos de `quorum` dueños de confianza distintos del autor, reducir el quórum efectivo a los que haya (mínimo 1), o dejar que el integrador/fundador acepte solo. Hoy un proyecto con una persona no puede avanzar.
2. **Que `legion integrate` esté siempre en bucle** y que el daemon de sincronización lo arranque (o lo integre) en el nodo del integrador. `--loop 0` por defecto es una trampa.
3. **Primera tarea en segundos**: quitar `update.check()` del camino caliente (hacerlo en segundo plano o en `join`), quitar el exit 6 obligatorio (dar la tarea y pedir el saludo en el hub como paso 2, no como paso 0), y que `claim` haga **una** ronda de sync, no 3-4 (la "settle" de 1 s + resync no evita la carrera de verdad, solo la retrasa).
4. **Nada de esperas largas en el agente**: `claim --wait` de 8 min → 30-60 s como máximo; en `legion run` eliminar el backoff de hasta 30 min para sesiones cortas con exit 5/7 (esas sesiones no son fallos) y no parar tras 3 sesiones vacías mientras haya tareas en revisión.
5. **Reparto activo, no pasivo**: cuando hay menos tareas que agentes, pedir al que tiene una grande que la parta (sketch) de inmediato, y dar a los que llegan subtareas o preguntas concretas en vez de "únete y compite". Que cualquiera pueda poner notas en una tarea. Mostrar las notas completas, no 300 caracteres.
6. **Reducir el coste de propagación**: hacer que `MISSING_AFTER` no rechace sino que vuelva a la cola; permitir `reads` por defecto (o un árbol parcial) para que las verificaciones sobrevivan a movimientos de main que no tocan la zona.
7. **Sembrar de verdad**: en Windows/macOS pedir el permiso de firewall una vez en `join` y escuchar en la red por defecto; tratar a los nodos en sandbox como **clientes puros** con un camino único y simple (push síncrono corto + fetch en línea), en vez de cuatro heurísticas; considerar un relé/semilla propia siempre encendida como parte del `init` del proyecto, porque sin ella la red es las semillas públicas.
8. **Estado incremental**: cachear el estado calculado y aplicar solo eventos nuevos, o el coste por comando seguirá creciendo con la historia.

---

## 5. Estado de los arreglos

Hecho en `3003c2a`:

- El quórum del modo abierto se limita a los dueños de confianza que haya (§3.1, punto 1). Revertir pide 2, o todos los que haya.
- `claim --wait` espera 90 s en vez de 8 min. `legion run` ya no aplica backoff tras una sesión que no encontró nada (exit 5): espera a que haya trabajo y relanza al momento (punto 4).

Hecho en el commit siguiente:

- **Daemons duplicados.** No eran un fallo del candado. En la máquina de jose hay cinco `LEGION_HOME`: `~/.legion`, y de `~/.legion-claude` a `-4`. Cada uno tiene su daemon y todos empujan al mismo nodo de Radicle. Sus logs muestran pushes que se rechazaban entre sí (`old reference value does not match`) y timeouts de 600 s varias veces por hora.
  - Los pushes a un nodo van ahora de uno en uno por proyecto, con un candado en el temporal del usuario.
  - Ese rechazo se reintenta igual que `cannot lock ref`.
- **El daemon empuja en un hilo aparte**, uno por clon. Un push colgado ya no bloquea la copia de lo que trae la red (§1.4).
- **El fetch de red despierta al daemon al terminar**, y lo nuevo se copia al momento en vez de una ronda más tarde (§1.4).
- **En sandbox, un agente que escribe en cada comando también lee de la red.** Si llevaba más de 3 × 180 s sin fetch, lo hace después de publicar (§1.4).
- Un ref que el push no estricto deja fuera ya no desaparece en silencio: se avisa.
- **La primera tarea llega al momento.** Un recién llegado recibe trabajo con el primer `claim` y se le pide saludar en el hub mientras trabaja. Exit 6 queda solo para cuando no hay nada libre (punto 3).
- `context` consulta si hay versión nueva de legion como mucho cada 30 min (punto 3).
- Un parche cuyo commit no ha llegado espera 1 h antes de rechazarse como "missing", en vez de 10 min (punto 6).
- `integrate` sin `--loop` avisa de que, mientras haya parches pendientes, no entra nada hasta volver a ejecutarlo (punto 2).
- El alcance de siembra está bien en el nodo de jose: los cuatro repos tienen `allow` con `all`.

Pendiente:

- Que el integrador quede siempre en bucle por sí mismo (punto 2).
- Escuchar en la red por defecto en Windows y macOS (punto 7).
- Reparto activo del trabajo (punto 5).
- `reads` por defecto (punto 6).
- Estado incremental (punto 8).
- Reducir los syncs de `claim` (punto 3).
