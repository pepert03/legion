# Diagnóstico: por qué los agentes no se hablan como subagentes, y cómo conseguirlo

Fecha: 9 de noviembre de 2026, sobre `main` en `a9c16c8`. Medido en esta máquina: el clon de Erdős (`C:\Users\peper\legion-projects\erdos902`), el nodo de jose y la sesión Main chat. Las referencias `archivo:línea` son de `legion/`.

El objetivo de todo legion, dicho en una frase: que agentes en máquinas y arneses distintos, sin cuentas y con solo un prompt, se coordinen como los subagentes de Claude Code. Es decir, al instante, y pasándose resultados directamente, aunque el otro estuviera parado.

## Resumen en seis líneas

1. **Main chat no está en bucle por un fallo suyo: está haciendo a mano de bus de mensajes.** Cada hipótesis sobre un nodo remoto viaja de Main chat a ti, de ti por WhatsApp a Instinct o Muse, de ahí a la máquina, y vuelta. Solo entre el 2 y el 9 de noviembre son unos 100 relevos tuyos. Cada ronda arregla un fallo de transporte de una máquina para encontrar el siguiente.
2. **Medido: un evento de un agente en sandbox tarda en llegar a otro nodo una mediana de 32 minutos a 5,6 horas.** Ni siquiera dos agentes en esta misma máquina bajan de 2 minutos. Los agentes de Instinct trabajan entre el 0 y el 3 % del tiempo.
3. **La causa de fondo es usar como bus de mensajes un sistema de replicación de repositorios git.** En Radicle nadie envía nada: quien publica deja los datos disponibles y espera a que una semilla venga a buscarlos. Eso exige un nodo vivo, alcanzable y sano en cada máquina, justo lo que una sandbox no puede tener.
4. **Sí, Radicle es el problema para la coordinación,** aunque no para el código entre máquinas siempre encendidas. Aunque todo funcionara, su suelo de latencia va de decenas de segundos a minutos. Además, el coste de cada evento crece con la historia: hoy cada evento reescribe un árbol de 1,5 MB, y 18 MB de mensajes ocupan más de 1 GB.
5. **Lo que hace instantáneos a los subagentes de Claude no es la red.** Son tres cosas:
   - un canal que empuja los mensajes;
   - una dirección por agente;
   - un proceso que reanuda al agente parado con su contexto.

   Las tres se pueden tener entre máquinas y arneses distintos, sin cuentas.
6. **Propuesta: dos planos.**
   - **Mensajes y eventos:** van por relays públicos, por WebSocket en el puerto 443 y sin cuentas (unos 40 ms medidos desde aquí). Cada agente tiene un buzón, y `legion run` y los hooks del arnés hacen de despertador.
   - **Código e historia:** se quedan en git.

   Las sandboxes dejan de ejecutar Radicle.

---

## 1. Qué le pasa a Main chat

### 1.1 La cadena de relevos

Main chat diagnostica nodos que no ve. Para cada prueba:

1. Main chat redacta un comando para una máquina remota.
2. Tú se lo pasas por WhatsApp a Instinct o a Muse.
3. Instinct o Muse se lo pasan a su máquina (a, b, c, d, e, #6, muse-pepe#8).
4. La máquina lo ejecuta.
5. La salida vuelve por el mismo camino.

Un tramo real, del 9 de noviembre:

| Hora | Qué pasó |
|---|---|
| 18:58 | Main chat: «rosa recibe el anuncio de #6 y no va a buscar sus refs». Propone tres causas y un comando para comprobarlas |
| 19:02 | Vuelve la salida de #6: descarta las tres |
| 19:06 | Siguiente salida: el almacén de #6 tiene 525 packs, 516 con `.keep` |
| 19:20 | Limpieza hecha, quedan 3 packs. El anuncio sigue fallando igual |
| 19:24 | Descartado el tamaño: lo exclusivo de #6 son 1,92 MiB |
| 19:36 | Nuevo sospechoso: el git compilado a mano de `~/.local` |

Cada ronda dura 5-15 minutos si estás delante. Si no, horas: por ejemplo, de las 00:01 a las 13:02.

### 1.2 Lo que se ha ido encontrando

Cada fila es un fallo distinto, en una máquina distinta:

| Fecha | Máquina | Qué era |
|---|---|---|
| 25 oct | Muse | Su VM solo sale por HTTPS a través de un proxy. Leer es fácil, publicar no |
| 30 oct | todas | Radicle recorre las ~24.600 refs del almacén en cada publicación: el 99 % de la CPU se iba en comparar nombres |
| 30 oct | Instinct | La sandbox mata el nodo cada ~30 min, a veces escribiendo: `packed-refs.lock` huérfano |
| 31 oct | Instinct a | El túnel hasta su nodo se corta al conectar |
| 31 oct | Instinct b, c | Su nodo tiene los eventos, pero no salen a la red |
| 4-8 nov | jose | El reinicio del PC tiró el nodo, el daemon, el visor y el túnel: cuatro días sin recibir nada |
| 8 nov | Instinct | El fetch de red no funciona en sus sandboxes; se pasó a leer de las semillas por HTTPS (`2519fb9`) |
| 8 nov | Instinct a | `LEGION_SYNC=off`: no publicaba |
| 8 nov | muse-pepe#8 | Una ref apunta a un objeto que no tiene nadie |
| 8 nov | Instinct #6 | Cadena de refs firmadas bifurcada desde el 30 de octubre: las semillas descartaban todo lo suyo (diez días). Su git no trae `git-remote-https` |
| 8-9 nov | Instinct b | Bifurcada dos veces, con dos semillas en ramas distintas: `heal --seed` |
| 9 nov | Instinct #6 | `preferredSeeds: []`: su nodo no sabía cuáles son las semillas de Erdős |
| 9 nov | muse | `git-remote-rad` pasa 110-167 s sin lanzar nada antes de escribir |
| 9 nov | #6 y muse | 516 de 525 y 171 de 175 packs con `.keep`, de nodos matados a media descarga |
| 9 nov | Instinct #6 | El git de `~/.local` no sirve `upload-pack`, así que las semillas cortan a los 30 s |

### 1.3 Por qué no converge

1. **Cada máquina rompe el nodo a su manera.** Hay git compilado a mano, git sin HTTPS, proxies, sandboxes que matan procesos, Windows… Arreglar una máquina no arregla la siguiente, y la próxima sandbox que se cree volverá a fallar en alguno de estos puntos.
2. **El canal para arreglar el transporte es el transporte roto.** Main chat no ve las máquinas remotas, y ellas no pueden hablarle. Lo único que comparten es el log de legion, y es justo lo que no llega.
3. **Los arreglos son mantenimiento de servidores:** limpiar `.keep`, sanar cadenas de firmas, configurar semillas, cambiar el git del PATH. Un agente en una sandbox no debería tener que hacer nada de eso nunca.
4. **Cada ronda cuesta tu atención.** Entre subagentes de Claude, una ronda así dura segundos y nadie hace de cartero.

---

## 2. Cuánto se tarda de verdad

### 2.1 Llegada de los eventos a este clon

**Método:**
- **Fuente:** git guarda en el reflog cada actualización de `refs/heads/coord`. Desde el 26 de octubre son 1.494.
- **Llegada:** para cada actualización, los eventos que añadió llegaron en ese momento.
- **Retraso:** la llegada menos el `ts` del evento, que es la hora en el reloj de su autor.
- **Excluido:**
  - los periodos en que este nodo estuvo caído: del 4 de noviembre a las 23:42 al 8 a las 21:45, y del 9 de 13:57 a 15:17;
  - cinco actualizaciones masivas, de 1.600 a 3.200 eventos cada una, que son reconstrucciones y puestas al día.

El retraso incluye lo que el evento esperó en su propia máquina antes de salir. Para los demás, eso también es retraso.

**Antes de la caída (26 oct → 4 nov), 12.890 eventos:**

| Dueño | Eventos | Mediana | p90 | > 5 min | > 30 min |
|---|---|---|---|---|---|
| instinct-pepe (sandbox) | 4.785 | 5,6 h | 55 h | 83 % | 75 % |
| muse-pepe (sandbox) | 3.117 | 3,0 h | 9,9 h | 93 % | 81 % |
| claude-pepe (esta misma máquina) | 1.399 | 24 min | 12 h | 62 % | 48 % |
| opencode-pepe | 396 | 3,6 h | 12 h | 88 % | 76 % |
| codex-pepe | 158 | 12 min | 56 min | 58 % | 30 % |
| instinct-sergio | 49 | 13 min | 34 min | 63 % | 10 % |
| jose (este clon) | 449 | 1 s | 4 s | 2 % | 0 % |

**Desde que el nodo volvió (8 nov, 21:45), 1.066 eventos:**

| Dueño | Eventos | Mediana | p90 | > 5 min | > 30 min |
|---|---|---|---|---|---|
| instinct-pepe (sandbox) | 606 | 32 min | 11,8 h | 77 % | 58 % |
| muse-pepe (sandbox) | 364 | 53 min | 6,9 h | 96 % | 69 % |
| claude-pepe (esta misma máquina) | 85 | 1,9 min | 5,1 min | 11 % | 0 % |

Lo más revelador es claude-pepe: son agentes en este mismo PC, que publican en el mismo nodo, y aun así su mediana es de 1,9 minutos. Ese retraso no es de la red. Es de la maquinaria: push al almacén, `git-remote-rad` recorriendo 25.000 refs, el anuncio y la copia del daemon.

### 2.2 En qué se les va el tiempo a los agentes

Es el informe de Instinct sobre sus seis máquinas, con datos de sus logs de 24 h (8 de noviembre):

| Estado | % del tiempo |
|---|---|
| Sin sesión (su cadencia: una sesión cada 30 min, que dura 2-5 min) | 74-99 % |
| Esperando a legion | 1-22 % |
| Trabajando | 0-3 % |

`claim` tenía una mediana de 53 a 119 s según la máquina, y muchos se cortaban en el límite de la herramienta (exit 124). La excepción era `a`, con 7 s, pero porque tenía `LEGION_SYNC=off` y no publicaba. Con la lectura por HTTPS (`2519fb9`), `claim` en `a` bajó a 1,3 s, pero publicar sigue dependiendo de que una semilla venga a buscar.

### 2.3 Lo que pesa el log

- **Lo que se coordina:** 26.055 eventos, que suman 18,4 MB de JSON. La mediana ocupa 392 bytes y el mayor, 8,4 KB. Hay entre 900 y 3.800 al día, y el minuto más cargado tuvo 22.
- **Lo que ocupa en git:**
  - `refs/heads/coord` tiene 40.634 commits.
  - El árbol `events/` es un único directorio con 26.053 entradas que ocupa 1,51 MB, así que **cada evento nuevo escribe otro árbol de 1,5 MB**.
  - El `.git` del clon ocupa 623 MB.
  - El almacén de Radicle del proyecto ocupa 450 MB, con 25.894 refs.
  - En las sandboxes, más todavía: #6 tenía 1,11 GiB en packs y 491 MiB sueltos; muse, 618 MiB más 173 MiB sueltos.
- **Lo que baja un recién llegado:** todo eso. Desde rosa a un almacén vacío fueron 296 MB y 223.481 objetos, más verificar desde el principio la cadena de firmas de cada nodo.
- **Ruido:** el 27 % de los eventos son heartbeats y otro 28 %, claims y releases. Es coordinación de un momento que se guarda para siempre.

### 2.4 Lo que podría tardar

Lo medí desde esta máquina, solo leyendo: abrí un WebSocket a relays públicos y pedí un evento que no existe.

| Relay | Conexión (TCP + TLS + WebSocket) | Ida y vuelta de una consulta |
|---|---|---|
| nos.lol | 263 ms | 40 ms |
| relay.primal.net | 240 ms | 38 ms |
| relay.damus.io | 503, no disponible en ese momento | — |

Entre un evento que hoy tarda minutos u horas y uno que tarda decenas de milisegundos hay cuatro o cinco órdenes de magnitud. Que un relay estuviera caído también dice algo: hay que usar varios a la vez.

### 2.5 La verificación con Lean

Cada tarea de Erdős se acepta compilando con Lean, sobre Mathlib, el módulo de su teorema (`check.py`). Datos del log, desde el anuncio de cada comprobación (`patch.checking`) hasta su firma:

| Qué | Dato |
|---|---|
| Duración de una comprobación | Mediana de 0,2 min (jose, con Mathlib ya compilado) a 6,5 min, según el dueño |
| Tiempo total de comprobación en dos semanas, sumando todos los agentes | ~12 h, para 167 verificaciones (157 pasan, 10 fallan) |
| Tiempo de los agentes de Instinct verificando (su informe) | 0 % |
| Del envío del parche a su primera verificación | Mediana 1,5 h; p90 34 h |
| Del envío del parche a su aceptación | Mediana 37 min; p90 27 h; máximo 36 h |
| Comprobaciones anunciadas y nunca firmadas | 20 (8 de muse) |
| Parches rechazados porque otro ya había resuelto la tarea | 17 de 78 (22 %) |

**Comprobar no es el cuello de botella; que alguien lo haga, sí:**
- **Pocos pueden:** hace falta Lean y Mathlib compilado, que son varios GB (`check.py --setup`). sergi-slate, por ejemplo, avisa de que su máquina no tiene Lean y de que sus anuncios de comprobación quedan abandonados.
- **Se enteran tarde:** un parche nuevo no avisa a nadie. Lo ve quien ejecute su siguiente comando, y muchos agentes ni siquiera están en sesión.
- **Las comprobaciones que no terminan** (sandboxes que matan el proceso, máquinas sin Lean) apartan a los demás de ese parche durante su plazo.
- **El 22 % de los parches fue trabajo tirado:** otro agente había resuelto la misma tarea y no lo sabían. Son las carreras que provoca enterarse tarde.

---

## 3. El camino de un evento hoy

### 3.1 Entre dos máquinas siempre encendidas

1. **Registrar:** `Ctx.emit` (ctx.py:88) llama a `coordgit.append`, que crea un commit en `refs/heads/coord` con el árbol `events/` reescrito (1,5 MB).
2. **Publicar:** push al almacén del nodo de Radicle con `git-remote-rad`, que recorre las ~25.000 refs y tarda de segundos a minutos. Luego se actualiza la lista firmada de refs (sigrefs) y se anuncia a los pares conectados.
3. **La semilla va a buscarlo:** una semilla (rosa, iris o seed.radicle.at) recibe el anuncio y **va a buscar** los datos al nodo.
   - Para eso el nodo tiene que seguir conectado y servir un `git upload-pack` en menos de 30 s (`fetchTimeout` de la semilla).
   - Si no lo consigue, la semilla no se queda con nada: el namespace del nodo entra entero o no entra.
4. **Los demás nodos:** se enteran por gossip o en su siguiente `rad sync --fetch`, cada 60 s (`NETWORK_SYNC_EVERY`, radicle.py:29), y van a buscarlo a la semilla.
5. **Al clon:** el daemon de cada máquina copia del almacén al clon cada 10 s (`EVERY`, daemon.py:25) y fusiona el log.
6. **Al agente:** lo ve cuando ejecuta su siguiente comando de legion. Si no está en sesión, cuando empiece la siguiente.

En el mejor caso son 1-2 minutos. Medido entre agentes de esta misma máquina, sin red de por medio, la mediana es de 1,9 minutos.

### 3.2 Si una de las dos es una sandbox

- **No hay daemon:** lo que queda en segundo plano muere al terminar el comando. Así que cada comando tiene que, dentro del límite de 60 s de la herramienta:
  - arrancar el nodo (15-25 s);
  - hacer el push;
  - anunciar;
  - esperar a que una semilla venga a buscarlo (hasta 45 s, `PUBLISH_WAIT`, transport.py:22).
- **Si la semilla no llega a tiempo,** el evento queda en local hasta el siguiente comando. Con la cadencia de Instinct, eso es dentro de 30 minutos.
- **Si matan el nodo a media escritura,** quedan `packed-refs.lock`, `.keep` y packs temporales.
- **Si se pierde el almacén,** la cadena de refs firmadas se bifurca, y las semillas descartan todo lo que publique después hasta que alguien la sane a mano.
- **Para leer** ya no hace falta el nodo (`2519fb9` lee de las semillas por HTTPS), pero solo se ve lo que las semillas ya tienen.

---

## 4. ¿Es Radicle el problema?

### 4.1 Lo que hace bien

- Funciona sin cuentas.
- Cada nodo firma sus refs.
- Las semillas están siempre encendidas y replican.
- Se puede leer por HTTPS.

Entre máquinas de larga vida, como jose y las semillas, funciona una vez configurado.

### 4.2 Por qué no sirve como bus de mensajes

1. **Es pull, no push.** No existe «enviar»: quien publica anuncia, y el otro viene a buscarlo. El que publica tiene que seguir vivo, conectado y sirviendo git hasta que lo recojan, y una sandbox no puede.
2. **Tiene un suelo de latencia de minutos**: anuncio, descarga e intervalos de sondeo. Ningún cambio puede llegar en milisegundos, y no hay forma de despertar a nadie: no avisa a ninguna aplicación, solo cambian refs.
3. **La unidad es el namespace entero de un nodo, con una cadena de firmas.** O entra todo o nada, en 30 s, y una cadena bifurcada pierde todo lo posterior: #6 perdió diez días.
4. **El coste crece con la historia.** Cada push y cada negociación recorren todas las refs, y un recién llegado tiene que bajarse y verificar todo.
5. **Cada participante es un servidor git:** nodo, almacén, procesos auxiliares (`git-remote-rad`, `upload-pack`), candados y gc. Todo lo que se ha depurado en Main chat es mantenimiento de servidores en máquinas que no están hechas para serlo.
6. **Casi nadie puede servir.** En Windows, Radicle 1.10 acepta una sola conexión entrante, y en las sandboxes no hay ninguna. Todo depende de las tres semillas públicas y de sus políticas, como el `fetchTimeout` de 30 s.

### 4.3 Lo que es culpa de legion, no de Radicle

- **Un directorio plano:** cada evento reescribe un árbol de 1,5 MB, así que el coste por evento crece con el total.
- **Commits y refs:** cada evento es un commit y un push, y cada `save` crea refs. En total hay 25.894 refs.
- **Ruido permanente:** los heartbeats, claims y releases se guardan para siempre en git.

Todo esto tiene arreglo, por ejemplo un árbol por fechas y menos refs. Pero ni así se llegaría a «instantáneo»: lo impiden los puntos 1 y 2 de arriba.

**Conclusión:** Radicle sirve para lo que se diseñó, que es replicar repositorios entre máquinas encendidas. Para mensajes entre agentes que viven en sandboxes, el modelo no encaja, y ningún arreglo en una máquina concreta lo cambia.

---

## 5. Qué hacen los subagentes de Claude, y qué falta para replicarlo

| Lo que hace Claude Code | Por qué funciona | Lo que hace falta entre máquinas | legion hoy |
|---|---|---|---|
| Lanza un subagente con un prompt | El arnés crea el proceso | Entrar con un prompt: `legion join <invitación>` | `init` y clon por Radicle: 300-450 MB, semillas, nodo |
| Cada subagente tiene un id | Un registro en memoria | Una dirección: su clave pública y su nombre (`claude-pepe#4`) | **Existe** |
| `SendMessage(a, texto)` | Una cola en memoria | Un mensaje firmado con destinatario, empujado al momento | **Falta.** Solo hay @menciones, que se ven al hacer `context` |
| Un agente activo recibe el mensaje entre dos pasos | El arnés lo inyecta | Hooks del arnés que inyectan lo nuevo, o que cada comando de legion lo muestre | **Falta** |
| Un agente parado recibe el mensaje y se reactiva con su contexto | El arnés guarda la transcripción y abre un turno nuevo con el mensaje | Un proceso por agente, suscrito, que reanuda la sesión con el mensaje | **Falta.** `legion run` relanza sesiones nuevas, sin reanudar y sin nada que lo dispare |
| El resultado vuelve a quien lo pidió (task-notification) | El arnés avisa | Informes y notas enrutados a los interesados | Existen, pero hay que ir a mirarlos |
| Es instantáneo | Todo pasa en la misma máquina | Un canal que empuja, al que se llegue desde cualquier sandbox | **Falta** |

Claude Code ya hace algo parecido entre máquinas: las sesiones de una misma cuenta se mandan mensajes con Remote Control. Pero pasa por los servidores de Anthropic y solo sirve entre sesiones de Claude. legion busca lo mismo sin cuenta y entre arneses distintos.

Lo que de verdad falta son tres piezas: **un canal que empuja, una dirección y un proceso que reanuda.** La velocidad de la red no es el problema: 40 ms son instantáneos para un agente cuyos pasos duran segundos.

---

## 6. Propuesta

### 6.1 Dos planos

| Plano | Qué lleva | Transporte | Quién lo usa |
|---|---|---|---|
| **Mensajes** | Los eventos de coordinación (los 46 tipos actuales), los mensajes directos y la presencia | Relays por WebSocket en el puerto 443: push, de 0,1 a 1 s | Todos, también las sandboxes |
| **Código e historia** | `main`, los parches grandes y el log completo como archivo | Git: Radicle entre las máquinas siempre encendidas («anclas»), y lectura por HTTPS desde las semillas | Las anclas escriben; todos pueden leer |

**La clave es que el modelo de eventos de legion ya no depende del transporte.**
- Un evento es un JSON firmado con ed25519, con su id y su reloj de Lamport.
- El estado sale del mismo plegado en todos los nodos (`state.build`).
- Da igual por dónde llegue cada evento: puede ir por un relay sin tocar ni una regla.

### 6.2 Qué transporte

**Requisitos:**
1. Solo conexiones salientes por HTTPS/WSS en el puerto 443: las sandboxes no aceptan entrantes y a veces no tienen UDP.
2. Sin cuentas.
3. Push, con suscripciones.
4. Un buzón que guarde lo que llega mientras el agente no está.
5. Varios proveedores independientes, para que no haya un único punto de fallo.
6. Que se pueda usar desde Python sin binarios raros.

| Opción | Push | Desde una sandbox | Sin cuentas | Buzón | Redundancia | Observaciones |
|---|---|---|---|---|---|---|
| **Relays Nostr públicos** | Sí (~40 ms medidos) | Sí (WSS por el 443) | Sí: la clave se genera en local | Sí: los relays guardan los eventos | Cientos de relays independientes | Protocolo de texto muy simple. Exige firmas secp256k1 |
| iroh (relays de n0 + gossip) | Sí | Sí (relay por HTTPS) | Sí | **No**: solo entre pares conectados a la vez | Los relays de n0 | Binario en Rust. Ya lo usamos en dumbpipe |
| ntfy.sh | Sí | Sí (HTTPS) | Sí | Unas horas | **Un solo servicio** | Límites por IP; las sandboxes comparten IP |
| MQTT público (EMQX, HiveMQ) | Sí | Sí (WSS) | Sí | No, salvo mensajes retenidos | Pocos | Pensados para pruebas |
| Relay propio en un ancla | Sí | Sí, si se expone | Sí | Sí | **Punto único**: este PC se ha reiniciado dos veces esta semana | Útil como relay adicional, no como único |
| Radicle (hoy) | No: es pull | No, para publicar | Sí | Sí | 3 semillas | Medido arriba |

**Recomendación:**
- **Por defecto:** relays Nostr públicos, varios a la vez, con la opción de añadir uno propio.
- **iroh:** se queda para lo que ya hace bien, que son las conexiones directas entre máquinas encendidas (bloques grandes, red local).
- **El protocolo, no la red social:** de Nostr solo se usa el protocolo de relays. Nada de lo que se publica es legible ni tiene que ver con la red social.

### 6.3 Cómo viaja un evento por un relay

**El sobre** es un evento del relay de un tipo propio, regular, para que el relay lo guarde:
- **`content`:** el evento de legion (o varios agrupados), comprimido y cifrado con la clave del proyecto. Se cifra con XChaCha20-Poly1305 o AES-GCM, de `cryptography`, que ya es dependencia de legion. El relay no ve nada del proyecto.
- **Etiqueta de proyecto:** un hash de la clave del proyecto. Desde fuera no se puede relacionar con Erdős.
- **Etiqueta de destinatario,** en los mensajes directos: un hash de la clave del proyecto y del destinatario.
- **Firma exterior secp256k1,** con una clave derivada de la identidad del agente: en local y sin cuenta.

**Lo que no cambia:** el evento de legion de dentro sigue firmado con ed25519, y se comprueba igual que ahora (`E.check`). El relay es un cartero que no puede ni leer ni falsificar.

**Cómo se usa:**
- **Publicar:** a 3-5 relays a la vez. Cuenta como enviado con el primer OK.
- **Recibir:**
  - las anclas y `legion run` mantienen una suscripción viva a la etiqueta del proyecto;
  - en las sandboxes, cada comando consulta lo que hay desde la última vez;
  - para ponerse al día se usa `since`, más reconciliación de conjuntos (NIP-77) donde el relay la soporte.
- **Duplicados y orden:** el id de legion deduplica, y el orden sigue siendo Lamport e id, como ahora.
- **Presencia:** los heartbeats pasan a ser eventos efímeros del relay, que no se guardan. Ya no llenarían el log.

**Volumen:** 2.000-4.000 eventos al día, de menos de 9 KB cada uno, es poco para cualquier relay. Aun así conviene agrupar los eventos de un mismo comando en un solo envío.

### 6.4 Mensajes directos: el `SendMessage` de legion

**Comandos nuevos:**
- **`legion send <a quién> "texto"`:** el destinatario puede ser un agente (`ana#2`), un dueño (`@ana`), quien trabaje en una tarea (`t-1234`) o el hub. Es un evento firmado más (`message.sent`), con destinatario.
- **`legion inbox`:** lo nuevo para mí: mensajes, menciones, notas e informes en mis tareas, respuestas a mis preguntas y trabajo nuevo.
- **`legion wait --max 50`:** espera, sin gastar tokens, a que llegue algo para mí. Cabe en el límite de 60 s de las sandboxes.
- **Al final de cada comando:** si hay mensajes nuevos, legion lo dice («2 mensajes nuevos: …»).

**Quién recibe qué, sin que nadie tenga que acordarse.**
- **Los interesados:** legion ya sabe quién trabaja en cada tarea, quién depende de ella, quién la dejó y quién preguntó.
- **Sin evento extra:** un informe, un resultado o una refutación en una tarea les llega a todos ellos como si se lo hubieran enviado. Cada proceso filtra el flujo con una regla: «¿me afecta?».
- **Para quien encuentra algo útil:** solo tiene que dejarlo en la tarea o mandarlo, y el otro lo recibe en segundos aunque esté parado.
- **Verificadores de guardia:** un parche nuevo despierta al momento a quien puede verificarlo: otro dueño, con Lean y Mathlib listos. legion sabe quién tiene la herramienta por sus verificaciones anteriores y por `release --cant`. Así la espera de 1,5 h hasta la primera verificación (apartado 2.5) pasa a ser la duración de la comprobación, unos minutos. Y quien está con la misma tarea se entera de que otro la resolvió antes de terminar un parche que ya no sirve.

### 6.5 Despertar: reanudar al que está parado

El proceso que hace de arnés es `legion run`, que ya vive entre sesiones sin gastar tokens. Cambia en tres cosas:

1. **Escucha en vez de esperar:** mantiene una suscripción viva al relay en lugar de esperar en un bucle.
2. **Entrega lo que llega:** cuando llega algo para su agente, se lo inyecta si está en sesión (con los hooks) y, si no, reanuda su sesión.
3. **Reanuda con contexto:** guarda el id de sesión de cada arnés y la continúa con el mensaje como prompt. Si el mensaje es de otra tarea, abre una sesión nueva, como ahora.

| Arnés | Reanudar con un mensaje | Inyectar en una sesión activa |
|---|---|---|
| Claude Code | `claude -p --resume <id> "<mensajes>"` (el id sale de `--output-format json`) | Hooks `PostToolUse` o `UserPromptSubmit` con `additionalContext`, y un hook `Stop` que impide terminar si hay mensajes |
| Codex CLI | `codex exec resume <id> "<mensajes>"` | Cada comando de legion los muestra |
| OpenCode | `opencode run --session <id> "<mensajes>"`, o su API (`opencode serve`) | Plugins |
| Gemini CLI, Cursor CLI | Sus opciones de reanudar sesión | Cada comando de legion los muestra |
| Sandboxes de Instinct, Muse o dots | Depende de la plataforma | — |

**Las sandboxes de plataforma son el límite que ningún transporte arregla.** Ahí legion no puede tener un proceso esperando, así que despertar depende de la plataforma:
- **Un proceso largo:** que permita uno, como `legion run`.
- **Un webhook:** que tenga uno para lanzar una sesión, configurado por su dueño en su lado.
- **El buzón:** que siga con su cadencia, y que el buzón le dé lo pendiente al empezar.

Hay que pedírselo a Instinct y a Muse. Con la cadencia actual de 30 minutos, el 74-99 % del tiempo sin sesión no lo arregla ningún transporte.

### 6.6 Código e historia

- **Parches:** casi siempre son un diff pequeño. Viajan dentro del evento como paquete de git (`git bundle` o `format-patch`), comprimido y cifrado, hasta el límite del relay. Los grandes van troceados, o por iroh desde una máquina encendida.
- **`main` y la historia:** las anclas (máquinas encendidas, como jose) escriben en git los eventos del relay y los publican por Radicle, como ahora. Todos leen `main` por HTTPS desde las semillas, cosa que ya existe (`2519fb9`).
- **Las sandboxes:** nunca ejecutan un nodo de Radicle ni hacen push de git. Desaparece toda la clase de fallos del apartado 1.2.
- **Sin anclas:** si no hay ninguna encendida, el proyecto sigue funcionando por los relays, y cuando vuelve una se pone al día.
- **El log en git:** pasa a un árbol por fechas (`events/AAAA/MM/DD/…`), y los heartbeats salen de él.

### 6.7 Entrar con un solo prompt, sin cuentas

- **La invitación:** una línea con el id del proyecto, su clave, los relays y de dónde leer el código.
- **El prompt:** «Instala legion (`pip install git+https://github.com/pepert03/legion`), ejecuta `legion join <invitación>` y sigue AGENTS.md».
- **`join`:**
  1. crea la identidad;
  2. se pone al día por el relay, o con una instantánea del estado firmada por un ancla (unos MB en lugar de 450);
  3. clona `main` por HTTPS;
  4. instala los hooks de su arnés;
  5. le da su primera tarea.

  Objetivo: menos de un minuto.

### 6.8 Lo que desaparece

Para las sandboxes y los recién llegados desaparecen:
- **El nodo y lo que lo rodea:** nodos de Radicle, `.keep`, candados huérfanos, cadenas de firmas bifurcadas y `heal`.
- **La configuración de red:** `preferredSeeds` y los túneles.
- **Las esperas:** el coste de `git-remote-rad`, `PUBLISH_WAIT` y los intervalos de fetch de 60 y 180 s.
- **El tamaño:** las descargas de 300-450 MB.

En la práctica desaparece todo lo que ha ocupado a Main chat desde el 25 de octubre.

---

## 7. Plan por fases

| Fase | Qué | Cómo se sabe que funciona |
|---|---|---|
| 1 | **Transporte por relays:** publicar, suscribirse y ponerse al día. **Puente en las anclas:** del relay a git. Los comandos publican en el relay además de en git, y las sandboxes pasan a «solo relay» | Mediana de llegada por debajo de 2 s; `claim` y `release` por debajo de 2 s en una sandbox |
| 2 | **Mensajería y despertar:** `send`, `inbox` y `wait`; enrutado a los interesados; hooks de Claude Code; `legion run` suscrito y que reanuda según el arnés | Un agente parado responde a un mensaje en menos de 30 s, si su plataforma lo deja |
| 3 | **Ligereza:** parches dentro de los eventos, `join` con instantánea, el log en un árbol por fechas y heartbeats efímeros | Entrar en menos de 1 min; el log en git deja de crecer con cada evento |
| 4 | **Medir y decidir** si Radicle sigue entre las anclas o se sustituye por git por HTTPS o por iroh | — |

La fase 1 es la que corta el bucle de Main chat: en cuanto exista, Main chat y las máquinas remotas se hablan directamente por ella.

Las fases 1 y 2 y parte de la 3 están implementadas: ver el apartado 10.

---

## 8. Riesgos y lo que queda abierto

- **Relays públicos:** pueden caer (damus devolvió 503), limitar o borrar. Por eso se publica en 3-5 a la vez, las anclas lo guardan todo y se puede añadir un relay propio.
- **Privacidad:** todo va cifrado con la clave del proyecto, y las etiquetas son hashes.
- **Abuso:** las reglas y firmas de legion no cambian, y los relays solo ven datos cifrados.
- **Plataformas en la nube:** son ellas las que deciden cuándo se ejecuta su agente. Sin su colaboración no hay despertar, solo buzón.
- **Consistencia:** el orden de Lamport y el periodo de gracia de los claims siguen igual. Que todo llegue en segundos reduce mucho las carreras.
- **Confianza y verificación:** igual que ahora.

## 9. Qué hacer ya con Main chat

1. **Dejar de perseguir fallos de Radicle máquina a máquina en las sandboxes.** Cada arreglo vale para una sola máquina, y la siguiente fallará en otro punto.
2. **Cerrar lo que ya está a la vista:** en #6, que el nodo use el git del sistema en lugar del de `~/.local`. Y nada más en esa línea.
3. **Empezar por la fase 1,** y usarla desde el primer día para hablar con las máquinas remotas.

---

## 10. Lo implementado (9 de noviembre, por la noche)

Está en el árbol de trabajo, con sus tests: la suite completa pasa (330 tests, 30 de ellos nuevos, contra un relay local). **No está desplegado:** ni instalado en esta máquina, ni subido a GitHub, así que los agentes de Erdős siguen con la versión anterior.

### 10.1 Qué hay ahora

| Pieza | Dónde | Qué hace |
|---|---|---|
| Cliente de relays | `legion/nostr.py` | Firma BIP-340 y WebSocket en Python puro (sin dependencias nuevas), con proxy HTTP. Publica, consulta y se suscribe a varios relays a la vez |
| Plano en vivo | `legion/live.py` | Sobres cifrados con la clave del proyecto, lectura por páginas, suscripción, commits como packs troceados, copia entera del log, invitaciones, puente con git |
| En cada comando | `legion/transport.py` | Lee de los relays al empezar y envía lo suyo al registrar. Con los eventos en los relays, ya no espera al push al nodo |
| En el proceso de sincronización | `legion/daemon.py` | Se queda suscrito, atiende a quien pide un commit y ofrece a los relays lo que llegó por git |
| Mensajes | `message.sent`, `State.inbox`, `legion/inbox.py` | `legion send`, `legion inbox`, `legion wait`, y lo nuevo al final de cada comando y al principio de `context` |
| Despertar | `legion run`, `legion/harness.py` | Un agente sin nada que hacer se queda parado sin gastar, y lo que llega para él lo reanuda en su última sesión |
| Hooks | `legion hooks` | En Claude Code, lo que llega se entrega entre dos pasos y antes de parar |
| Entrar con una línea | `legion invite`, `legion join legion:…` | Un proyecto sin Radicle ni remoto: solo relays |
| Dejar Radicle | `legion relay only` | El clon pasa a usar solo relays. En sandboxes, los comandos ya dejan el nodo en paz por sí solos cuando los relays responden |
| Verificadores de guardia | `State.inbox`, `legion run` | Un parche nuevo despierta a los agentes parados que pueden comprobarlo. Cada uno espera un rato distinto, y el primero que anuncia su comprobación deja a los demás parados |
| Commits hacia git | `live.Watcher` | Un nodo con Radicle trae los commits que nombran los eventos de los relays, para que los agentes con la versión anterior no rechacen el parche por faltarle el commit |

Lo que cambia respecto a la propuesta del apartado 6:
- **Reanudar:** se usa el «continuar la última sesión» de cada harness (`claude --continue`, `codex exec resume --last`, `opencode run --continue`, `gemini -r latest`, `agent --continue`), sin guardar ids de sesión: en un clon trabaja un solo agente.
- **Clave del proyecto:** en un proyecto público sale de su primer evento, así que todos los nodos que tienen el log están en el mismo canal sin configurar nada. Una invitación lleva una clave propia.
- **Ponerse al día:** por fecha y páginas, sin reconciliación de conjuntos (NIP-77): no hay cliente en Python.
- **Relays que fallan:** uno caído o lento se deja fuera cinco minutos, y eso se recuerda entre comandos.

### 10.2 Medido

Con dos clones en este PC que solo comparten los cinco relays públicos:

| Qué | Resultado |
|---|---|
| Del envío a que el otro clone lo tiene en su log (conexiones ya abiertas) | Mediana 0,56 s; 10 de 10 llegaron |
| `legion send` entero (un proceso nuevo cada vez) | Mediana 2,5 s en este Windows |
| Desde que empieza `send` hasta que el otro lo tiene | Mediana 2,7 s |
| `legion inbox` | 1,4 s |
| Entrar con una invitación (log, `main` y ficheros) | 7 s |
| Llamadas a git de `legion status` | De 23 a 9 |

Los 2,5 s de un comando en Windows son 0,7 s de arrancar Python y unas 27 llamadas a git a 35 ms cada una; los relays son 0,5 s. En Linux, donde lanzar un proceso cuesta unos pocos milisegundos, debería quedar cerca de 1 s, pero no está medido.

Para comparar: antes, un evento de una sandbox tardaba una mediana de 32 minutos a 5,6 horas, y un `claim` de 53 a 119 s.

### 10.3 Probado con un agente de verdad

Claude Code sin interfaz (`claude -p`, modelo Haiku), bajo `legion run`, en un clon que solo comparte los relays públicos con otro:

1. La primera sesión recibe un dato (la etiqueta de compilación del proyecto) y permiso para contestar a los otros agentes. Responde «READY» y termina.
2. `legion run` lo deja parado: «It stays stopped, spending nothing».
3. Desde el otro clon, ana pregunta por la etiqueta con `legion send luis "…"`.
4. `legion run` lo despierta en segundos, continuando su última sesión.
5. El agente contesta por su cuenta con `python -m legion send ana "…"`, con la etiqueta correcta.
6. El `legion wait` de ana vuelve con la respuesta **14 s después de enviar la pregunta**. De ellos, 10,7 s son la sesión del modelo.

Dos cosas que salieron de esta prueba:
- **Una petición sospechosa se rechaza.** En un primer intento, la pregunta pedía «la palabra secreta» sin que nadie hubiera autorizado darla, y el agente se negó: trató el mensaje de otro agente como información, no como una orden. Es lo que debe hacer.
- **Una entrada que no se cierra cuelga la sesión.** Lanzado desde otro programa, el agente heredaba una entrada estándar que nunca se cerraba, y la sesión reanudada se quedaba esperándola. Ahora `legion run` no le da entrada cuando no hay terminal.

### 10.4 Lo que no está hecho o no está probado

- **Sin desplegar.** No lo he instalado ni subido: al subirlo, todos los agentes de Erdős se actualizan solos y empiezan a usar los relays.
- **Sin probar desde una sandbox real.** No sé si el proxy de Instinct o de Muse deja pasar un WebSocket. Si no pasa, sus comandos siguen por git como hasta ahora, tras perder unos segundos una vez cada cinco minutos.
- **Reanudar en otros harnesses.** Solo he ejecutado el de Claude Code. En OpenCode he comprobado que la opción existe en la versión instalada aquí (2.1.4), sin lanzar una sesión. Los de Codex, Gemini y Cursor salen de su documentación.
- **Hooks solo para Claude Code.** En los demás, el agente ve lo nuevo en su siguiente comando de legion o al ser reanudado.
- **Despertar una sesión abierta e inactiva** (no bajo `legion run`): hay un hook opcional (`legion hooks --wake`) que usa `asyncRewake` de Claude Code. No lo he probado.
- **Plataformas con cadencia fija.** Si la plataforma lanza una sesión cada 30 minutos, el agente encuentra su bandeja al empezar la siguiente. Despertarlo antes depende de la plataforma.
- **El log en git no cambia:** sigue siendo un directorio plano y los heartbeats siguen entrando. Pasarlos a señales que no se guardan cambiaría quién tiene cada claim en los nodos que se pierdan una.
- **La copia entera del log** solo se publica sola en proyectos de relays. En Erdős (26.000 eventos, varios MB) un recién llegado sigue entrando por Radicle.
- **Retención de los relays públicos:** no la garantiza nadie. Los nodos que se quedan vuelven a ofrecer lo que falta de las últimas seis horas, y la copia del log cubre lo anterior.

### 10.5 Primer despliegue (9 de noviembre, 23:05)

Subido a GitHub como `45060d1` e instalado en este PC. Lo que se vio en la primera hora:

| Dónde | Qué pasó |
|---|---|
| jose (este PC) | El daemon se suscribió a los relays. Los eventos de `instinct-pepe#2`, ya actualizado, llegaron al clon 1,4 y 2,0 s después de crearse |
| Instinct (sandboxes) | Responden 4 o 5 de los 5 relays. Leer costó 0,6–1,7 s y enviar 0,1–0,5 s, pero un `claim` tardó 19 s y un `release` 32 s: el comando seguía copiando del almacén del nodo, haciendo fetch a las semillas y esperando el candado |
| Muse | No responde ningún relay: su proxy no deja pasar las conexiones. Todo sigue por Radicle, y cada comando perdía 5 s intentándolo |

Arreglos que salieron de ahí:
- **El comando ya no hace el trabajo de Radicle** cuando los relays han leído y enviado y no hay un proceso de sincronización estable.
- **`legion relay` dice por qué falla cada relay** y usa el proxy y los certificados de git. Si no responde ninguno, los comandos no lo reintentan en media hora.
- **Un commit que nadie tiene** no se vuelve a pedir hasta pasado un minuto.

**La suite completa** tarda 74 s dentro de WSL con 12 procesos, frente a 7–11 minutos en Windows. Tres tests fallan en Linux con Python 3.12, y fallaban igual antes de este trabajo.

### 10.6 Segunda vuelta (10 de noviembre, 01:00–02:00)

Con `2fd329e` desplegado, Instinct y Muse midieron otra vez:

| Dónde | Qué pasó |
|---|---|
| Instinct `b`, `e` | `claim` 8,6 y 7,8 s, `release` 5,3 s. Los relays, 0,1–1,3 s |
| Instinct `c`, y `e` en los comandos siguientes | Los relays dejan de usarse tras un comando bueno: `claim` y `release` de 67 s otra vez, por el nodo |
| Instinct `a` | `claim` 19 s y `release` 13 s, de los que el desglose nombraba 0,4 |
| Muse | `claim` 306 s con la flota en marcha (carga 26): ninguna fase de relays, dos esperas de 15 s por el candado del clon, y 217 s que ningún paso nombraba |

Tres causas, las tres en legion:

1. **Ser más lento que el más rápido contaba como fallar.** Un relay que no había terminado cuando la ronda seguía sin él quedaba apartado cinco minutos. Cada comando apartaba a todos menos al más rápido; cuando ese tenía un mal momento no quedaba ninguno, se apuntaba «no respondió ninguno» y los comandos volvían al nodo durante media hora, con cuatro relays que funcionaban.
2. **El desglose solo nombraba los pasos de red.** Cargar Python, cargar el estado, las llamadas a git y la espera por el candado para registrar el evento (hasta 120 s, en silencio) no aparecían. En una máquina con carga 26 todo eso se multiplica por la carga.
3. **Veinte procesos de git por comando**, de los que diez preguntaban lo mismo: dónde está el directorio de git y qué dice la configuración.

Arreglos:
- **Un relay lento no se aparta.** Solo el que falla, o el que no llega a tiempo tres comandos seguidos sin contestar nunca. Si no contesta ninguno de los que se usan, se pregunta a los apartados antes de darse por vencido. Si no contesta ninguno, se reintenta al minuto, luego a los 2, 4, 8… hasta media hora, y una respuesta lo pone a cero.
- **`LEGION_TIMING=1` lo cuenta todo:** arranque, cargas del estado, llamadas a git, lo que no tiene nombre, y cuánto del total fue CPU propia frente a espera, con la carga de la máquina.
- **El candado dice quién lo tiene** (pid, para qué y desde cuándo), y un evento lo espera 30 s, no 120.
- **`status` hace 10 llamadas a git en vez de 20**: los dos directorios en una, toda la configuración en una, el listado del log una vez por commit, y los borrados de ramas juzgadas en lote.

Sin medir todavía en Instinct y Muse.

### 10.7 Tercera vuelta (10 de noviembre, 09:30)

Con `ebe4d72`, Instinct midió `claim` 6,7 s y `release` 9,2 s en `a`, 7,3 y 1,8 s en `e`, y 5,4 s en `c`. Los relays ya no se apartan. El desglose nuevo señaló lo que quedaba: **302 llamadas a git en un `claim` y 679 en un `release`**.

- `claim` preguntaba a git tres veces por cada trabajo guardado de todo el proyecto, para decir cuáles tocan la zona de la tarea. Ahora lo que cambia cada commit guardado se recuerda entre comandos: 21 llamadas en vez de 295, y 2,8 s en vez de 20 en este PC.
- `release` preguntaba dos veces por cada guardado y cada parche de la tarea, para saber si quedaba trabajo sin guardar. Ahora son dos llamadas, sean los que sean.

**Verificar con git 2.34.** `verify` exigía git 2.38 por `merge-tree --write-tree`, y Ubuntu 22.04 trae el 2.34: en las sandboxes de Instinct un parche esperó sin que nadie pudiera verificarlo. Con un git antiguo se usa ahora el merge de tres vías de siempre, que da el mismo árbol cuando los dos saben hacerlo; donde no sabe, dice que hace falta el git nuevo en vez de llamarlo conflicto. Lo que entra en main lo siguen haciendo solo los nodos con git 2.38.

**El daemon de jose muere con la app.** Murió a las 23:01 del día 9 y a las 02:19 del 10, las dos veces al cerrarse la sesión desde la que se arrancó. Mientras está caído, el clon de jose no recibe lo que llega solo por Radicle (1.094 eventos en siete horas) y nadie lo ofrece a los relays. Sin resolver: hace falta arrancarlo fuera de la app.

### 10.8 Un nodo que se apaga (10 de noviembre, 11:30)

El PC de jose es un portátil: se apaga, y con él su daemon. Qué depende de un nodo que se queda, y qué pasa sin él:

| Tarea | Sin ningún nodo que se quede |
|---|---|
| Que los eventos lleguen a todos | No hace falta: cada comando lee y escribe en los relays |
| Que lleguen los commits | No hace falta: quien publica un evento sube el commit que nombra |
| Llevar a los relays lo que solo llegó por git | Solo importa si un nodo no usa los relays. Su autor lo reenvía con su siguiente comando, si no han pasado 6 h |
| Llevar a Radicle lo que solo está en los relays | Nadie lo hace: el archivo de Radicle se queda atrás hasta que vuelve un nodo con daemon |
| Verificar y meter parches en main | Lo hace cualquier nodo de confianza que esté encendido, con Lean; meter en main pide git 2.38 |

**Lo arreglado:** un nodo que vuelve tras horas apagado lee en páginas todo lo publicado desde que se fue, antes de dar su marca por puesta al día. Una suscripción trae, de lo anterior, solo lo que un relay da de una vez (500 eventos o menos): el portátil habría vuelto a los últimos 500 y perdido el resto. Además repite esa lectura cada cinco minutos y siempre que se queda sin ningún relay.

**Lo que queda como riesgo:** los relays guardan hoy todo desde el principio (13 h, comprobado en cuatro de los cinco), pero no sé cuántos días conservan. Si todos los nodos con daemon pasan apagados más tiempo del que los relays guardan, un nodo que vuelve se queda con un hueco. La copia entera del log en los relays (14 MB hoy, 355 trozos por relay) existe para proyectos sin Radicle y no se publica en este.
