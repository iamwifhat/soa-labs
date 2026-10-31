Технічні файли звітів ЛР2 і ЛР3. Підсумкові документи — ЛР2_SOA_КН-61мп_Заєць.docx і ЛР3_SOA_КН-61мп_Заєць.docx у корені.

Код лабораторних: ../../lab2 і ../../lab3 (кожна запускається окремо: docker compose up -d --build; порти однакові).

Порядок перезбирання:
1. Вивід перевірок зберігається в ../lab2/outputs і ../lab3/outputs (scripts/demo.py, scaling_demo.py, saga_demo.py).
2. node draw_figures.mjs            — схеми (figures/*.png), з перевіркою, що текст не виходить за рамки.
3. python3 render_terminals.py && node capture_terminals.mjs — «вікна терміналу» з виводу перевірок (terminals/*.png).
4. Скріншоти Consul/Traefik/RabbitMQ: ../lab2/capture_ui.mjs, обрізані копії — screens/*.png.
5. Python з python-docx (codex runtime): python3 lab2_report.py, python3 lab3_report.py.
   Шаблон — ЛР1_SOA_КН-61мп_Заєць.docx: титульна сторінка, стилі SOA, колонтитули; змінюється лише номер роботи.
6. QA: soffice --headless --convert-to pdf, pdftoppm; сторінки переглядаються в render/.
