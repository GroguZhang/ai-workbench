# Echoes of 1939: A RAG-Driven Computational Musicology Agent 🌊

> An interdisciplinary AI agent that bridges computational musicology, Retrieval-Augmented Generation (RAG), and constructivist pedagogy to provide an immersive, multimodal learning experience for the *Yellow River Cantata*.

---

## 📌 Project Overview
In traditional music appreciation education, teaching macro-historical vocal suites often faces significant pain points: lack of emotional resonance, high barriers to professional musical terminology (e.g., Canon, Ostinato), and weak individualized interactive feedback.

**"Echoes of 1939"** solves this by breaking the traditional "one-way instillation" model. By deeply simulating the persona of the original composer, **Xian Xinghai**, this AI agent creates a cross-temporal, first-person dialogue. It seamlessly integrates music theory, historical context, and emotional values, transforming users from passive listeners into active explorers.

---

## ⚙️ System Architecture
The agent is designed using a modular architecture, separating the orchestration layer, retrieval engine, and client interface.

```text
[User Input]
   │
   ▼
[Intent Router (State Machine)] ──► State 1: New Movement Request
   │                                  ├──► Trigger Python Workflow (asymmetric_card_generator.py)
   │                                  └──► Return Rich Media Card + Hidden Question
   │
   ├──► State 2: Deep Exploration (Locked Mode)
   │      ├──► Semantic Query Generation
   │      ├──► [Vector Database] ◄── Retrieve ── [Chunked Musicology Corpus]
   │      └──► LLM Synthesis (Persona: Xian Xinghai) + Pedagogical Prompting
   │
   └──► State 3: Knowledge Summarization
          └──► Extract JSON Context ──► Trigger TreeMind Plugin ──► Return Mind Map
```

## 🚀 Key Technical Innovations

### 1. Asymmetric Information Delivery Workflow (Frontend/Backend Decoupling)
This is a pioneering approach in educational AI applications. The Python-based workflow strictly decouples the output data into two distinct streams:
* **Frontend (For the User):** `rich_card` containing multimodal visual assets (16:9 epic oil painting style) and matched high-quality audio/video streams.
* **Backend (For the AI):** `hidden_analysis` serving as an invisible teaching guide.

> This asymmetric mechanism allows the LLM to act as an experienced educator—holding the "lesson plan" and asking suspenseful, guided questions without directly exposing the answers, perfectly realizing the **Socratic method** of heuristic teaching.

### 2. Context-Aware Finite State Machine (FSM) Prompting
To address the common industry issue of LLMs repeatedly calling tools during complex multi-turn instructions, this project innovatively introduces **FSM logic** into the system prompt. It sets state locks to prevent endless loops and forces silent RAG retrieval when in **"Discussion Mode"**.

---

## 🧠 Advanced RAG Data Pipeline
To ensure the historical and musicological accuracy of the generated content, a rigorous data engineering pipeline was established for the **20,000+ word corpus**:

1.  **Data Cleaning & Structuring:** Processed raw historical PDFs and music analysis papers into structured formats, removing OCR noise and standardizing musical terminologies.
2.  **Semantic Chunking:** Instead of naive fixed-length text splitting, applied **Context-Aware Semantic Chunking**. The corpus was divided by movements (1 to 8) and analytical dimensions to maintain complete context windows.
3.  **Metadata Tagging:** Each chunk was tagged with specific metadata `{"movement_id": int, "category": string}` to enable **Hybrid Search** (Vector Similarity + Metadata Filtering), ensuring the LLM never cross-contaminates the analysis of Movement 2 with the historical background of Movement 7.

---

## 📊 Human-in-the-Loop (HITL) Evaluation Framework
As a Computational Musicology project, standard LLM metrics are insufficient. A custom evaluation matrix was implemented during the gray-box testing phase:

*   **Musicological Alignment (Weight 40%):** Evaluates whether the LLM correctly interprets specific techniques (e.g., accurately explaining **Polyphonic Canon** without hallucinating).
*   **Persona Consistency (Weight 30%):** Measures adherence to the 1939 historical context and the composer's specific tone of voice.
*   **Pedagogical Efficacy (Weight 30%):** Assesses the quality of the `hidden_analysis` questions.

---

## 🛠️ Tech Stack
| Category | Tools & Technologies |
| :--- | :--- |
| **Core LLM** | Doubao (ByteDance) via Coze Platform |
| **Architecture** | RAG (Retrieval-Augmented Generation), Agentic Workflow |
| **Scripting** | Python (Async logic, fuzzy matching, robust error handling) |
| **Visual Gen** | Prompt Engineering for 16:9 Epic Oil Painting style |

---
**Developed by Zheren Zhang | Ph.D. Candidate in Performing Arts and Communication & Computational Musicology Explorer**
