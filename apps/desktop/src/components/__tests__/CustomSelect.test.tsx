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

  it('renders composed option labels instead of internal identifiers', () => {
    render(
      <CustomSelect value="job-123" onChange={() => {}} aria-label="Должность">
        <option value="job-123">{'Backend Engineer'} ({'Senior'})</option>
      </CustomSelect>
    );
    const trigger = screen.getByRole('combobox', { name: 'Должность' });
    expect(trigger).toHaveTextContent('Backend Engineer (Senior)');
    fireEvent.click(trigger);
    expect(screen.getByRole('option', { name: 'Backend Engineer (Senior)' })).toBeInTheDocument();
    expect(screen.queryByText('job-123')).not.toBeInTheDocument();
  });

  it('allows keyboard navigation when every option is disabled', () => {
    render(
      <CustomSelect value="missing" onChange={() => {}} aria-label="Недоступные варианты">
        <option value="a" disabled>A</option>
        <option value="b" disabled>B</option>
      </CustomSelect>
    );
    const trigger = screen.getByRole('combobox');
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('skips disabled options at both keyboard boundaries', () => {
    render(
      <CustomSelect defaultValue="a" aria-label="Вариант">
        <option value="first" disabled>Недоступный первый</option>
        <option value="a">Первый доступный</option>
        <option value="b">Последний доступный</option>
        <option value="last" disabled>Недоступный последний</option>
      </CustomSelect>
    );
    const trigger = screen.getByRole('combobox');
    fireEvent.keyDown(trigger, { key: 'End' });
    fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(trigger).toHaveTextContent('Последний доступный');
    fireEvent.keyDown(trigger, { key: 'Home' });
    fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(trigger).toHaveTextContent('Первый доступный');
  });

  it('closes on Tab and keeps uncontrolled selections', () => {
    render(
      <CustomSelect defaultValue="a" aria-label="Вариант">
        <option value="a">Первый</option>
        <option value="b">Второй</option>
      </CustomSelect>
    );
    const trigger = screen.getByRole('combobox');
    fireEvent.click(trigger);
    fireEvent.keyDown(trigger, { key: 'Tab' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: 'Второй' }));
    expect(trigger).toHaveTextContent('Второй');
  });
});
