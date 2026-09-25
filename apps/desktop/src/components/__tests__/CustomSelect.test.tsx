import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CustomSelect } from '../CustomSelect';

const Example = () => {
  const [value, setValue] = useState('all');
  return (
    <CustomSelect value={value} onChange={(event) => setValue(event.target.value)} aria-label="Статус">
      <option value="all">Все статусы</option>
      <option value="draft">Черновик</option>
      <option value="done">Завершено</option>
    </CustomSelect>
  );
};

describe('CustomSelect', () => {
  it('opens from the full trigger and selects an option', () => {
    render(<Example />);

    const trigger = screen.getByRole('combobox', { name: 'Статус' });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: 'Черновик' }));

    expect(trigger).toHaveTextContent('Черновик');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('supports keyboard navigation', () => {
    render(<Example />);

    const trigger = screen.getByRole('combobox', { name: 'Статус' });
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    fireEvent.keyDown(trigger, { key: 'Enter' });

    expect(trigger).toHaveTextContent('Черновик');
  });
});
