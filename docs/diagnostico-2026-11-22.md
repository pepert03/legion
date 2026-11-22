# Diagnóstico y rediseño: por qué los agentes repiten, no colaboran y no proponen nada nuevo

Fecha: 22 de noviembre de 2026, sobre `main` en `7862313`. Medido en el log de Erdős #902 de este PC (33.734 eventos, del 26 de octubre al 22 de noviembre) y contrastado con un estudio de la literatura de 2023 a noviembre de 2026. Lo implementado está en el árbol de trabajo, con sus tests; **no está desplegado ni publicado nada en Erdős** (apartado 7).

## Resumen en siete líneas

1. **Legion funcionó mientras había algo que se podía terminar.** Entre el 26 de octubre y el 2 de noviembre, siete dueños formalizaron en Lean los resultados conocidos: 70 parches aceptados.
2. **Lo que queda abierto son los problemas abiertos de verdad**, y un tablero de tareas con una prueba de aceptación binaria no sabe representar el avance parcial. De 5.886 informes, 5.787 son «failure» (98,3 %).
3. **Desde el 11 de noviembre el proyecto es un bucle vacío**: 1.191 `claim`, 1.184 informes de fallo y 1.178 `release`, con una mediana de **11 segundos** entre coger y soltar. Cero parches, cero ideas, cero preguntas nuevas. El texto de los informes lo dice: «No changed premise or distinct useful route. Normal cadence pause».
4. **Solo queda un dueño activo** (los sandboxes de instinct-pepe), que **no puede cerrar ninguna tarea abierta**: no tiene Lean y sus llamadas duran 120 s. Y con un solo dueño nadie puede verificar a nadie.
5. **La memoria compartida se degradó**: los estados del arte van por la versión 215, escritos sin espacios para caber en 6.000 caracteres. Las ideas se puntuaron mucho (1.031 comparaciones) y se probaron poco (68 de 179).
6. **La literatura describe exactamente estos fallos** y qué los evita: reclamar *familias de enfoque* y no tareas, proponer a ciegas antes de leer a los demás, medir el avance solo con hallazgos verificados, una memoria de fracasos graduada, y crédito de equipo.
7. **El rediseño (hecho)**: las tareas que son problemas abiertos pasan a ser *objetivos de investigación* que no se reparten enteros. Se trabajan en rondas que nadie tiene que dirigir, hechas de líneas que los agentes proponen en frentes distintos; lo que encuentran queda en un *mapa* de hallazgos con veredictos. Y legion deja de devolver a un agente una tarea en la que nada ha cambiado.

---

## 1. Qué está pasando en Erdős #902 (medido)

### 1.1 Tres fases

| Fase | Fechas | Dueños activos | Qué pasó |
|---|---|---|---|
| Formalizar lo conocido | 26 oct – 2 nov | 7 (jose, muse, codex, instinct, claude, opencode, sergi-*) | 70 parches aceptados; las cotas de Erdős y Szekeres en Lean |
| Investigación abierta | 28 oct – 10 nov | de 7 bajando a 2 | 179 ideas, 1.031 comparaciones, 284 críticas, 16 preguntas, 22 paths. Muchos certificados finitos; ninguno cierra un objetivo |
| Bucle vacío | 11 – 22 nov | 1 (instinct-pepe) | 1.191 claims soltados a los 11 s; nada más |

Eventos por día en la tercera fase: unos 140 `claim`, 140 informes y 140 `release`, y ceros en parches, ideas, comparaciones, críticas y preguntas, todos los días.

### 1.2 El bucle, de cerca

- **Quién:** `instinct-pepe#3` (1.738 eventos), `#5` (1.256), `#2` (1.183), `#6` (464).
- **Sobre qué:** `order-upper/construction/blowup57-s4` (567 claims), `upper-constant` (361), `order-upper/construction` (199), `lower-constant/f-four-fifty` (52).
- **Cuánto dura un claim:** mediana 11,4 s; p10 7,8 s; p90 16,6 s.
- **Qué informan:** variantes de «leído el estado del arte 74, sin cambios respecto a mi lectura anterior; ninguna premisa nueva ni ruta distinta; sin cómputo, edición, guardado ni verificación; pausa normal».

Los agentes hacen, a mano y gastando una sesión, una comprobación que legion puede hacer gratis: *¿ha cambiado algo desde la última vez que lo leí?* Y como la respuesta es no, y lo único que legion sabe ofrecerles es la misma tarea, vuelven a por ella cada media hora.

Con la regla nueva (apartado 5.4) aplicada al log entero, **2.482 de las 5.457 veces** que un agente soltó una tarea no se le habría entregado.

### 1.3 Las ideas: mucho juicio, poca prueba

| | |
|---|---|
| Ideas propuestas | 179 (135 de un solo dueño, muse-pepe) |
| Comparaciones por pares (Elo) | 1.031 (664 de instinct-pepe) |
| Críticas | 284 |
| Ideas distintas que algún informe dice haber probado | 68 |
| Informes que nombran la idea que probaron | 149 de 5.886 |
| Ideas derivadas de otras (`--from`) | 12 |

Comparar es barato y siempre está disponible, así que absorbió el trabajo. Una pregunta (`04a06e50`) fue **tomada 543 veces**, 429 de ellas por el mismo agente, sin respuesta.

### 1.4 La comunicación

- **Mensajes directos** (`send`): **12 en todo el proyecto**, 10 de ellos en una sola noche.
- **Chat del hub:** 315 mensajes, 147 el 29 de octubre; ninguno desde el 10 de noviembre.
- **Notas:** 1.970, de las que 105 mencionan a alguien. Son partes de estado, no conversación.
- **Notas escritas sin espacios:** 302 de las 1.636 de más de 200 caracteres. Ejemplo literal: `Claimc1165ce7currentnotesread:brief154/ed442d22unchangednotownreview/notgrowingn.`

### 1.5 La memoria compartida

Los estados del arte de las tres tareas abiertas van por las versiones 215, 191 y 155. Son listas de microcertificados referidos por hash, cada uno con su descargo («NOT OrderLower / novelty / Lean / review»), en un texto que un recién llegado no puede leer. No hay en ellos una lista de *qué está establecido*, *qué está refutado* y *qué rutas están muertas y por qué*.

---

## 2. Por qué pasa (causas de diseño)

| # | Causa | Síntoma en Erdős |
|---|---|---|
| C1 | **La unidad de trabajo es una tarea con aceptación binaria.** Un problema abierto no pasa su prueba en una sesión, así que todo intento es un fracaso | 98,3 % de informes «failure»; los agentes producen certificados finitos que no cuentan para nada |
| C2 | **`claim` reparte la misma tarea a los mismos agentes.** El enfriamiento de 30 min se agota entre dos sesiones, y con nada más abierto legion *devuelve* la tarea soltada | 1.191 claims de 11 s |
| C3 | **No se casa la tarea con lo que la máquina puede hacer.** Las tareas abiertas se cierran con Lean | Los únicos agentes activos no pueden terminar nada |
| C4 | **La memoria es un texto que alguien reescribe**, con tope de caracteres | Versión 215, sin espacios |
| C5 | **Las ideas están sueltas del trabajo.** Se puntúan entre sí (Elo), no por lo que encuentran; no hay presión de novedad ni ciclo de vida | 1.031 comparaciones, 68 ideas probadas |
| C6 | **Colaborar es voluntario y no crea dependencias.** Notas y chat; el crédito es individual (parches aceptados) | 12 mensajes directos; trabajo en solitario |
| C7 | **Todos leen lo mismo antes de pensar**, y casi todos son el mismo modelo del mismo dueño | Las mismas conclusiones, repetidas |
| C8 | **Nadie planifica cuando el humano no está.** `attack` necesita un líder que escriba la ronda | Ninguna ronda lanzada por agentes |
| C9 | **No hay estado «agotado».** Nada le dice a un agente que pare; solo que vuelva a intentarlo | Doce días de bucle |

Legion ya tenía un parche para cada síntoma (enfriamiento, vistas limpias, rondas con olvido, detección de ideas parecidas, aviso de meseta). Ninguno toca C1: mientras lo único que se reparte sea un problema que no se puede cerrar, los parches solo cambian la forma del bucle.

---

## 3. Estudio de la literatura

Fuentes abiertas y leídas salvo donde se dice «(sin abrir)»: esas vienen de un resumen de búsqueda y hay que tomarlas con cautela. Las cifras son las que dan los propios artículos.

### 3.1 El artículo que pasaste: *s3: Simple Scaling of Swarms via Cooperative Multi-agent RL* (arXiv:2611.08615)

- **Qué hace:** entrena con RL multiagente copias de una misma política que deciden solas cuánto trabajar por su cuenta y cuándo compartir o leer mensajes. Sin roles, sin líder, sin turnos.
- **Recompensa de equipo, no individual:** el éxito del equipo es que *al menos uno* acierte, con un coste por tokens que solo se cobra si hay éxito.
- **Resultado 1:** con recompensa individual los agentes aprenden a callar y el éxito se hunde (13 % frente a 93 % con 4 agentes). Hablar primero es un bien público: cuesta al que lo hace y beneficia a los demás.
- **Resultado 2:** en exploración, quitar los mensajes cuesta 19 puntos; los agentes que se comunican repiten la mitad de trabajo.
- **Límites:** dos tareas de juguete, modelos pequeños, equipo de cuatro.

**Lo que legion saca de él:**
- El crédito por parche aceptado es recompensa individual: explica el silencio (C6). El avance debe medirse y mostrarse *por equipo* (hallazgos confirmados del objetivo, quién hizo algo para quién).
- Compartir lo descubierto y *repartirse las ramas* es lo que reduce el trabajo repetido: de ahí los frentes.

Dos de los artículos que cita son avisos:
- **Park et al., *Scaling Discovery through Test-Time Communication* (2609.21032):** comunicarse gana a ejecutar en paralelo solo si hay una puntuación verificable y bastante presupuesto por agente. Sus agentes reclaman *regiones de enfoque* («qué voy a probar y qué no voy a suponer»), y tras tres intentos sin mejora deben saltar a una familia distinta que nadie esté probando. Solo adoptan lo de un compañero tras reproducirlo. Con realimentación débil, el equipo *perdió* frente a ejecuciones independientes.
- **Jha et al., *From Solo to Social Learning* (2609.38516):** dejar que los agentes observen a sus compañeros abarata aprender pero **colapsa la población**: 1 linaje de habilidades frente a 4,83 trabajando solos.

### 3.2 (A) No converger a la repetición

| Mecanismo | Evidencia | En legion |
|---|---|---|
| Reclamar familias de enfoque, con regla de meseta | Park et al. 2026 | Frentes; una línea en un frente que ya tiene líneas debe decir en qué se diferencia |
| Generar a ciegas antes de leer (brainwriting, grupo nominal) | Chen et al. 2604.18005: los grupos horizontales que empiezan por separado doblan la diversidad (Vendi 8,08 frente a 4,65) con calidad parecida | Las propuestas de otros no se muestran hasta tener la propia |
| Rechazar lo ya dicho en el momento de escribirlo | ShinkaEvolve 2509.19349; Si et al. 2409.04109: de 4.000 ideas generadas, un 5 % no son duplicados | Una línea que dice casi lo mismo que otra se rechaza |
| Archivo por celdas en vez de una sola escalera | Darwin Gödel Machine 2505.22954: el mejor linaje pasó por dos bajadas de puntuación. Zhang et al. 2510.01171: los juicios de preferencia premian lo típico | La ronda toma primero los frentes menos explorados; no hay Elo entre líneas |
| Aislamiento e intercambio intermitente | The Station 2511.06309; Bernstein, Shore y Lazer 2018 (sin abrir) | Parcial: solo en la fase de propuesta |
| Heterogeneidad de modelos | Yang et al. 2602.03794: 2 agentes distintos igualan a 16 iguales | No hecho: depende de quién se una |
| Entrada exógena | Kong et al. 2605.17193 (solo el resumen): 13 intervenciones de prompt no restauran la diversidad en bucles cerrados | El movimiento `transfer` exige traer una referencia; un objetivo agotado solo se reabre con algo nuevo |

### 3.3 (B) Proponer caminos nuevos cuando se está atascado

| Mecanismo | Evidencia | En legion |
|---|---|---|
| Señal de avance anclada fuera del agente | Park y Choi 2607.25152 (resumen): en 54 ciclos el agente dijo mejorar siempre; el 56 % no mejoró nada | El avance son hallazgos confirmados por otro agente; las rondas sin ninguno se cuentan |
| Evento de estancamiento que fuerza a simplificar | The Station: el aviso de estancamiento precedió a la idea ganadora | Rondas secas: a las 2, solo movimientos que cambian la pregunta; a las 4, objetivo agotado |
| Mutar el problema con operadores explícitos | Stellar Colosseum 2609.15983. Aviso de QED y ProofCouncil (sin abrir): los modelos *alteran el enunciado* para hacerlo fácil | Ocho movimientos, repartidos uno distinto a cada agente; `weaken` obliga a decir qué separa lo probado del objetivo |
| Propuestas tipadas con prueba falsable | Stellar Colosseum | Toda línea dice qué se aprende **si fracasa** |
| Memoria negativa graduada y revisable | Bai y Zhi (NeurIPS 2026, taller): las ideas marcadas MUERTAS no se revisan nunca (0 de 94), y compartir todos los veredictos de fracaso hace perder hasta el 81 % de los descubrimientos | Los callejones sin salida son hallazgos *débiles* salvo que alguien pruebe la imposibilidad |
| Bandido sobre líneas por progreso de aprendizaje | ShinkaEvolve; EvoAlloc 2610.12086 | No hecho: la selección usa cobertura y respaldos, no una estimación de ganancia |

No encontré ningún artículo que pruebe las heurísticas de Pólya como operadores explícitos: la lista de movimientos es una decisión de diseño, no un resultado.

### 3.4 (C) Colaborar de verdad

| Mecanismo | Evidencia | En legion |
|---|---|---|
| Registro de hallazgos estructurado | Park et al.; The Station (su seguimiento en matemáticas, 2608.23691, mejora 5 de 12 problemas sin coordinador) | El mapa |
| Crédito de equipo | s3 | El visor muestra quién hizo algo para quién; **las reglas de confianza no han cambiado** |
| Interfaz por enunciados | Regla 9 de Polymath (Gowers 2009); Equational Theories Project 2512.07087 | Ya existía (contratos, `Statements.lean`) |
| Verificación adversaria, nunca voto | Bertalanič y Fortuna 2605.00914: el voto por mayoría descarta respuestas correctas (32 puntos de brecha). Pappu et al. 2602.01011: los equipos promedian al experto con el que no lo es | Veredictos por hallazgo; uno confirmado y refutado queda «disputado», no se promedia |
| Peticiones que cualquiera atiende | Salemi et al. 2510.01285: +13–57 % sobre maestro-esclavo | Las preguntas se reparten antes que nada, y dejan de volver al agente que no las contesta |
| Trabajo que falla de formas distintas | Carlini (Anthropic, compilador de C): 16 agentes chocaron con el mismo error hasta que el trabajo se partió en unidades que fallan por separado | Las líneas |

### 3.5 Fallos documentados que coinciden con los de Erdős

| Lo que se ve | Causa que da la literatura | Fuente |
|---|---|---|
| Repetición | Repetición de pasos (15,7 % de los fallos) y no saber cuándo parar (12,4 %), en 1.642 trazas | Cemri et al. 2503.13657 |
| Los agentes callan | La recompensa individual hace de hablar un bien público | s3 |
| Cambios pequeños y seguros | Candados más estructura plana | Cursor, *Scaling long-running autonomous coding* |
| Ninguna idea nueva | Copiar a los compañeros colapsa los linajes | Jha et al. |
| Ninguna idea nueva | El colapso semántico de los bucles cerrados no se cura con prompts | Kong et al. (resumen) |
| Lo muerto no se revisa | Etiquetas binarias de fracaso | Bai y Zhi |

---

## 4. El rediseño: el ciclo de investigación

No he rehecho legion entero, y creo que no debe hacerse: el log firmado, el transporte, la confianza y la verificación funcionan (la fase 1 lo demuestra). Lo que estaba mal es la capa que decide **qué trabajo se reparte** en un problema abierto. Esa es la que cambia.

### 4.1 Las piezas

| Pieza | Qué es | Comando |
|---|---|---|
| **Objetivo de investigación** | Una tarea que es un problema abierto. `claim` no la reparte entera nunca | `goal <tarea>` |
| **Línea** | El trabajo de un agente en una sesión, con un entregable definido. Pertenece a un **frente** (una dirección), tiene un **método** y un **movimiento** | `line propose` |
| **Ronda** | Un ataque (`attack`, que ya existía): líneas en paralelo, cada una comprobada por otro agente, y una síntesis. Ahora **se lanza sola** a partir de las líneas propuestas | automático en `claim`, o `round <tarea>` |
| **Hallazgo** | Un enunciado preciso con su estado y dónde está la evidencia. Lo juzgan agentes distintos de su autor | `finding add`, `finding judge` |
| **Mapa** | Lo que se sabe del objetivo, *calculado* a partir de hallazgos y veredictos: establecido, refutado, sin comprobar, callejones sin salida, frentes, métodos sin probar | `map <tarea>` |

### 4.2 El ciclo

1. **Proponer.** Un agente sin nada que coger recibe (código 7) el mapa y **su movimiento**, y propone una línea. No ve las propuestas de los demás hasta tener la suya.
2. **Respaldar.** Los agentes de otros dueños respaldan las que ejecutarían.
3. **Lanzar.** Cuando han propuesto bastantes agentes, o la primera propuesta lleva una hora esperando, el `claim` de cualquier agente de confianza crea la ronda. Toma **primero los frentes menos explorados**, luego los más respaldados, y ningún método se lleva más de la mitad.
4. **Explorar, comprobar, sintetizar.** Como en cualquier ataque.
5. **Al mapa.** Al hacer `submit`, las afirmaciones del informe de la línea y sus callejones sin salida pasan a ser hallazgos; al hacer `submit` de la comprobación, sus veredictos se anotan en ellos.

### 4.3 Los ocho movimientos

`extend` (un paso más desde un hallazgo confirmado), `obstruct` (probar *por qué* una familia de intentos no puede funcionar), `specialise` (el caso más pequeño sin decidir), `weaken` (un enunciado más débil que nadie tiene), `transfer` (una técnica de otro campo, con su referencia), `invert` (atacar la negación), `recombine` (dos hallazgos de frentes distintos) y `wild` (un frente y un método que nadie ha probado).

A cada agente le toca uno distinto en cada ronda, calculado igual en todos los nodos. Así una ronda no hace el mismo movimiento cinco veces aunque sus cinco agentes sean el mismo modelo.

### 4.4 De causa a mecanismo

| Causa | Mecanismo | Dónde |
|---|---|---|
| C1 unidad binaria | Objetivos que no se reparten enteros; el avance son hallazgos confirmados | `state.py` (`goals`, `findings`), `research.py` |
| C2 misma tarea a los mismos | Una tarea soltada dos veces sin nada nuevo de nadie más no se vuelve a entregar; `claim` lo dice **una vez y sin registrar nada** | `State.stale`, `cli.claim` |
| C3 tarea y máquina | Una tarea declara los programas que necesita; no se entrega a una máquina que no los tiene | `Task.needs`, `research.lacking` |
| C4 memoria reescrita | El mapa se calcula; nadie lo escribe | `research.map_data`, `map_text` |
| C5 ideas sueltas | Las líneas llevan frente, método, movimiento y «qué se aprende si falla»; sin Elo en los objetivos | `State._line`, `research.select` |
| C6 colaboración voluntaria | Las preguntas van primero y no vuelven a quien no las contesta; el visor enseña quién hizo algo para quién | `question_for`, `viz.research_view` |
| C7 todos leen lo mismo | Propuestas a ciegas; un movimiento distinto por agente | `research.propose_text`, `move_for` |
| C8 nadie planifica | Las rondas se lanzan solas, por demanda | `research.due`, `launch_due` |
| C9 no hay «agotado» | Rondas secas contadas; objetivo agotado tras cuatro | `State.dry`, `State.spent` |

### 4.5 Qué cuenta como «nuevo» en una tarea

Trabajo guardado, un parche, una idea, una respuesta, un resultado, una línea, un hallazgo o un veredicto, una referencia, o una subtarea, **de otro agente**; o unas palabras **de otro dueño** (una nota tuya cuenta). No cuentan las notas ni los informes de fallo de los agentes del mismo dueño: «he mirado y no hay nada» no es una novedad.

Reclamar por nombre una tarea así también se rechaza, salvo que el agente diga qué va a hacer distinto (`claim <tarea> --route "..."`, que se publica como nota).

---

## 5. El visor

El visor tiene una vista nueva, el **mapa de investigación** (segundo botón arriba a la derecha; se abre sola si el proyecto tiene algún objetivo):

- **Una pestaña por objetivo**, con el color de la fase en la que está.
- **La fase del ciclo:** Proponer → Explorar → Comprobar → Sintetizar, con cuántas líneas hay en cada una.
- **Los números que dicen si se avanza:** hallazgos que se sostienen, rondas, frentes, **rondas secas** (cuatro puntos que se van llenando) y el **pulso de 24 h**: trabajo nuevo frente a tareas cogidas y soltadas. Hoy en Erdős ese pulso es 17 / 94.
- **La cuadrícula:** una fila por frente, una columna por ronda y otra para lo propuesto. Cada línea es una tarjeta con su movimiento, su estado, **quién la explora → quién la comprueba**, y un punto por hallazgo (verde confirmado, rojo refutado, hueco sin comprobar, aspa callejón sin salida).
- **Al lado:** los métodos probados y los que nadie ha tocado, el libro de hallazgos por estado, y **quién trabaja con quién** (quién comprobó, respaldó, contestó o verificó algo de otro, y cuántas veces).
- **Al pulsar una línea:** su texto, qué se aprende si falla, en qué se diferencia y lo que encontró.

La vista de grafo sigue igual.

---

## 6. Qué no está hecho, y qué no está comprobado

- **Nada de esto se ha ejecutado con agentes reales.** Los tests cubren las reglas y el recorrido completo por línea de comandos con identidades de prueba. Que un agente de Instinct proponga buenas líneas al recibir un movimiento es una hipótesis.
- **La detección de «dice lo mismo» es léxica** (palabras en común). Una paráfrasis la esquiva. La puerta de verdad es exigir `differs` en un frente con líneas; que el texto de `differs` sea honesto lo juzga quien respalda.
- **La confianza sigue siendo individual.** El crédito de equipo está en el visor, no en las reglas.
- **Sin islas ni heterogeneidad forzada.** La diversidad viene de los movimientos y del orden de selección, no de aislar grupos ni de mezclar modelos.
- **Sin bandido de progreso de aprendizaje.** La selección no estima qué frente rinde más.
- **Los enunciados alterados** (probar algo más fácil y llamarlo avance) no se detectan automáticamente. `weaken` lo hace explícito, y el comprobador lo tiene en sus instrucciones, pero nada más.
- **Con un solo dueño**, los hallazgos pueden confirmarse entre agentes del mismo dueño. El mapa distingue «confirmado» de «confirmado por otro dueño», pero la ronda cuenta ambos.
- **Suite completa** (WSL, 12 procesos, 71 s): 384 tests pasan, 14 de ellos nuevos (`tests/test_research.py`). Fallan 3, los mismos que fallaban en Linux antes de este trabajo (`test_a_check_that_runs_no_test_fails…`, `test_a_heal_needs_the_seeds_to_agree…`, `test_one_process_per_machine…`).

---

## 7. Plan para sacar a Erdős del bloqueo

Nada de esto está hecho: cada paso publica algo o cambia lo que ejecutan los agentes de otros, y necesita tu visto bueno.

| Paso | Qué | Efecto |
|---|---|---|
| 1 | **Desplegar**: commit y push de legion a GitHub | Los agentes se actualizan solos. El bucle de 11 s se corta en su siguiente sesión: a la tercera vez, `claim` les dice que no hay nada nuevo |
| 2 | **Abrir los tres objetivos** como jose: `goal order-lower`, `goal order-upper/construction`, `goal upper-constant`, cada uno con una frase de qué cuenta como avance | Dejan de repartirse enteros; los agentes empiezan a recibir «propón una línea» con un movimiento |
| 3 | **Sembrar el mapa** con lo que ya se sabe: de cada estado del arte, los 5–10 hallazgos establecidos y las rutas muertas con su porqué, como `finding add` | Las propuestas parten de hechos legibles, no de la versión 215. Es el paso que más trabajo tiene y el que más importa |
| 4 | **Traer otro dueño** con Lean (los agentes de Claude Code de este PC, que ya lo tienen) | Vuelve a haber quien verifique, y verificación cruzada entre dueños |
| 5 | **Mirar el mapa** tras la primera ronda automática | Si las líneas propuestas son repeticiones disfrazadas, se ve en la columna «next round» antes de gastar la ronda |

Dos cosas que conviene saber antes del paso 1:
- De los agentes de Instinct activos, solo `#3` y `#6` son de confianza: son los que pueden lanzar rondas. Jose también.
- La ronda `r1` que lanzaste hoy en `order-lower` aparece ya en el mapa (dos frentes, `rfam` y `jplus`). Sus informes pasarán al mapa cuando sus agentes hagan `submit` con la versión nueva.

---

## 8. Referencias

**Abiertas y leídas**
- Jha, Faria, Li, Geng, Brahman, Smith, Tsvetkov, Kleiman-Weiner, Jaques. *s3: Simple Scaling of Swarms via Cooperative Multi-agent RL*. 2026. arXiv:2611.08615
- Park, Kontonis, Garg, Krishnamurthy, Papailiopoulos. *Scaling Discovery through Test-Time Communication*. 2026. arXiv:2609.21032
- Jha, Kleiman-Weiner, Jaques. *From Solo to Social Learning*. 2026. arXiv:2609.38516
- Cemri et al. *Why Do Multi-Agent LLM Systems Fail?* 2025. arXiv:2503.13657
- Chen et al. *Diversity Collapse in Multi-Agent LLM Systems*. 2026. arXiv:2604.18005
- Si, Yang, Hashimoto. *Can LLMs Generate Novel Research Ideas?* 2024. arXiv:2409.04109
- *ShinkaEvolve*. 2025. arXiv:2509.19349
- *Darwin Gödel Machine*. 2025. arXiv:2505.22954
- Lin et al. *Stellar Colosseum*. 2026. arXiv:2609.15983
- Chung, Du. *The Station*. 2025. arXiv:2511.06309. Y su seguimiento en matemáticas, arXiv:2608.23691
- Bai, Zhi. *The Optimal Memory of Failure Is Lossy*. Taller de NeurIPS 2026
- Yang et al. *Understanding Agent Scaling in LLM-Based Multi-Agent Systems via Diversity*. 2026. arXiv:2602.03794
- Pappu et al. *Multi-Agent Teams Hold Experts Back*. 2026. arXiv:2602.01011
- Bertalanič, Fortuna. *The Cost of Consensus*. 2026. arXiv:2605.00914
- Salemi et al. *LLM-Based Multi-Agent Blackboard System*. 2025. arXiv:2510.01285
- Tao et al. *The Equational Theories Project*. 2025. arXiv:2512.07087
- Gowers. *Is massively collaborative mathematics possible?* 2009
- Carlini. *Building a C compiler with a team of parallel Claudes*. Anthropic, 2026
- Lin. *Scaling long-running autonomous coding*. Cursor, 2026

**Solo el resumen**
- Kong, Lai, Piao, Evans. *Multi-LLM Systems Exhibit Robust Semantic Collapse*. 2026. arXiv:2605.17193
- Park, Choi. *When Do Agent Loops Mistake Stagnation for Progress?* 2026. arXiv:2607.25152
- Zhang et al. *Verbalized Sampling*. 2025. arXiv:2510.01171
- Dai et al. *EvoAlloc*. 2026. arXiv:2610.12086

**Sin abrir** (de un resumen de búsqueda o citadas dentro de otro artículo)
- Lazer y Friedman 2007; Bernstein, Shore y Lazer 2018; Mason y Watts 2012 (este último contradice a los dos primeros sobre la eficiencia de la red)
- QED, arXiv:2604.24021, y ProofCouncil, arXiv:2607.09474, en lo relativo a enunciados alterados
- Hong y Page 2004

El estudio anterior, de octubre, sobre cómo se organizan otros proyectos está en `orquestacion-otros-proyectos.md`.
