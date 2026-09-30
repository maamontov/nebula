# Nebula desktop design

Shared rules live in `src/styles/design-system.css`, loaded after the screen
styles. Page headers use `PageHeader`; dropdowns use `CustomSelect`.

- White/light or dark neutral canvas and sidebar, using theme variables.
- Blue highlights belong to the logo and interactive controls. Content and
  navigation icons use neutral colors.
- Page padding: 24 px above, 32 px horizontally; 24 px between sections.
  Narrow windows use 18–24 px gutters.
- Page title: 18 px; section title: 14 px; body: 13 px; labels, actions and
  metadata: 12 px. Larger numeric results remain deliberate exceptions.
- Fields: 40 px high, 9 px radius; inline question/criterion fields use the
  compact 32 px size. Long-form fields grow with their text.
- Full-width dropdown triggers, neutral animated menus, keyboard navigation,
  visible focus, and distinct disabled states.
- Sections use whitespace; repeated editable questions may use a quiet surface.
- Search stays above the interview list. Filtering preserves the toolbar and
  current results while loading, and accepts only the newest response.
- Desktop job templates retain navigation, template list and editor columns.
  Below 700 px the list stacks above the editor; navigation collapses to icons.
- Page transitions and micro-interactions respect reduced-motion preferences.

Change these shared rules instead of adding per-screen spacing or type overrides.
