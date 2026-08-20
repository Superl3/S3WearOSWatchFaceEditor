# Minimal WFF template

This is the smallest checked-in Wear OS Watch Face Format project shape used by Photo2WFF. The compiler copies this project shape, replaces `watchface.xml`, and links generated resources from a validated `scene.json`.

The template is intentionally resource-only (`android:hasCode="false"`) and targets WFF version 1. It is not the Vision Analyzer. A fixed scene is built from it before image analysis is connected.

