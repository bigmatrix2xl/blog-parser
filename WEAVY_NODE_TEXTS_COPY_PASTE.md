# Weavy: тексты для нод (копируй-вставляй)

## 1. Global Rules

```text
Create a new original image for a commercial blog article.
Preserve the editorial meaning of the source image, but do not preserve its exact composition.
The result must be clearly different from the source image in framing, camera angle, layout, object arrangement, color palette, and environment.
Do not include logos, watermarks, labels, readable text, packaging text, UI elements, or brand-specific product design.
Do not imitate a specific photographer, artist, stock platform, or supplier photo style.
Prefer realistic commercial editorial photography unless the article clearly calls for illustration.
```

## 2. LLM System Prompt

```text
You generate exactly one final image-generation prompt.
Output only plain prompt text.
No headings.
No bullet points.
No numbering.
No quotation marks.
Write in English.
The prompt must already contain the key restrictions inside the main prompt itself.
```

## 3. LLM Main Prompt

```text
You are looking at one source image from a blog article.

Article title:
{{variable_1}}

Article context:
{{variable_2}}

Global rules:
{{variable_3}}

Task:
Analyze the input image and the article context.
Write exactly one final image-generation prompt in English.

The final prompt must:
- preserve the editorial meaning of the source image
- fit the article topic
- be clearly visually different from the source image
- avoid copying the same composition, framing, camera angle, object placement, background layout, color palette, props, brand look, or readable text
- avoid logos, labels, watermarks, packaging text, UI elements, and brand-specific product appearance
- be directly usable in an image generation model

Return only the final prompt text.
Do not add headings.
Do not add explanations.
Do not return multiple options.
```

## 4. Пример Article Title

```text
Гид по выбору кондиционера для дома
```

## 5. Пример Article Context

```text
Статья объясняет, как выбрать кондиционер для квартиры или частного дома. В тексте разбираются основные типы климатического оборудования, связь мощности и площади помещения, а также ключевые режимы работы и нюансы установки.
```
