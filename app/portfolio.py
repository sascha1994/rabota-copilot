"""Compose a cover letter with the portfolio links configured by the candidate."""

import re

LETTER_LIMIT = 1800
MARKETPLACE = re.compile(r'маркетплейс|wildberries|\bozon\b|вайлдберриз|\bозон\b|карточ\w*\s+товар', re.I)
AI_ROLE = re.compile(r'нейросет|генератив|\b(?:ai|ии)\b', re.I)


def with_portfolio(body, direction, title, description, env):
    selections = {
        'web': [('GENERAL', 'Портфолио')],
        'web_ui': [('GENERAL', 'Портфолио')],
        'graphic': [('GRAPHIC', 'Портфолио по графическому дизайну')],
        'narrative': [('AI', 'Портфолио работ с нейросетями')],
    }.get(direction, [])
    if direction == 'graphic' and MARKETPLACE.search(title + '\n' + description):
        selections.append(('MARKETPLACE', 'Инфографика для маркетплейсов'))
    if direction in ('web', 'web_ui', 'graphic') and AI_ROLE.search(title):
        selections.append(('AI', 'Портфолио работ с нейросетями'))
    letter = body.strip()
    links = []
    for key, label in selections:
        url = env.get('PORTFOLIO_' + key, '').strip()
        if url and url not in letter and not any(line.endswith(url) for line in links):
            links.append(f'{label}: {url}')
    if links:
        letter += '\n\n' + '\n'.join(links)
    return letter
