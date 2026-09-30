import React, {
  SelectHTMLAttributes,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown } from 'lucide-react';

type CustomSelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, 'multiple' | 'size'>;

interface SelectOption {
  value: string;
  label: string;
  disabled: boolean;
}

function optionText(children: React.ReactNode): string {
  return React.Children.toArray(children).map((child) => {
    if (typeof child === 'string' || typeof child === 'number') return String(child);
    if (isValidElement<{ children?: React.ReactNode }>(child)) return optionText(child.props.children);
    return '';
  }).join('');
}

export const CustomSelect: React.FC<CustomSelectProps> = ({
  children,
  className = '',
  disabled = false,
  id,
  name,
  onChange,
  value,
  defaultValue,
  title,
  'aria-label': ariaLabel,
  'aria-labelledby': ariaLabelledBy,
  ...nativeProps
}) => {
  const generatedId = useId();
  const listboxId = `${generatedId}-listbox`;
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const nativeRef = useRef<HTMLSelectElement>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const [menuStyle, setMenuStyle] = useState<React.CSSProperties>({});
  const [localValue, setLocalValue] = useState(defaultValue);

  const options = useMemo<SelectOption[]>(
    () =>
      React.Children.toArray(children).flatMap((child) => {
        if (!isValidElement<React.OptionHTMLAttributes<HTMLOptionElement>>(child)) return [];
        const optionValue = String(child.props.value ?? '');
        const label = child.props.label ?? optionText(child.props.children);
        return [{ value: optionValue, label, disabled: Boolean(child.props.disabled) }];
      }),
    [children]
  );

  const selectedValue = String(value ?? localValue ?? options[0]?.value ?? '');
  const selectedIndex = options.findIndex((option) => option.value === selectedValue);
  const selectedOption = options[selectedIndex];

  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const desiredHeight = Math.min(options.length * 38 + 10, 280);
    const roomBelow = window.innerHeight - rect.bottom;
    const openAbove = roomBelow < desiredHeight + 12 && rect.top > roomBelow;
    const menuHeight = Math.max(0, Math.min(desiredHeight, (openAbove ? rect.top : roomBelow) - 14));
    const width = Math.min(rect.width, window.innerWidth - 16);
    setMenuStyle({
      position: 'fixed',
      left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)),
      top: openAbove ? Math.max(8, rect.top - menuHeight - 6) : rect.bottom + 6,
      width,
      maxHeight: menuHeight,
    });
  }, [options.length]);

  const openMenu = useCallback(() => {
    if (disabled || options.length === 0) return;
    updatePosition();
    setHighlightedIndex(selectedIndex);
    setIsOpen(true);
  }, [disabled, selectedIndex, options.length, updatePosition]);

  const choose = useCallback(
    (nextValue: string) => {
      const nativeSelect = nativeRef.current;
      if (value === undefined) setLocalValue(nextValue);
      if (nativeSelect) {
        nativeSelect.value = nextValue;
        onChange?.({
          target: nativeSelect,
          currentTarget: nativeSelect,
        } as React.ChangeEvent<HTMLSelectElement>);
      }
      setIsOpen(false);
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    },
    [onChange, value]
  );

  useEffect(() => {
    if (!isOpen) return;
    updatePosition();
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) setIsOpen(false);
    };
    const handleViewportChange = () => updatePosition();
    document.addEventListener('pointerdown', handlePointerDown);
    window.addEventListener('resize', handleViewportChange);
    window.addEventListener('scroll', handleViewportChange, true);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      window.removeEventListener('resize', handleViewportChange);
      window.removeEventListener('scroll', handleViewportChange, true);
    };
  }, [isOpen, updatePosition]);

  useEffect(() => {
    if (isOpen && highlightedIndex >= 0) {
      menuRef.current?.querySelector<HTMLElement>(`[data-option-index="${highlightedIndex}"]`)
        ?.scrollIntoView?.({ block: 'nearest' });
    }
  }, [isOpen, highlightedIndex]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'Tab') {
      setIsOpen(false);
      return;
    }
    if (event.key === 'Escape') {
      setIsOpen(false);
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (!isOpen) openMenu();
      else if (options[highlightedIndex] && !options[highlightedIndex].disabled) {
        choose(options[highlightedIndex].value);
      }
      return;
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp' && event.key !== 'Home' && event.key !== 'End') {
      return;
    }
    event.preventDefault();
    if (options.length === 0) return;
    if (!isOpen) openMenu();
    const direction = event.key === 'ArrowUp' || event.key === 'End' ? -1 : 1;
    const edgeIndex = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : null;
    setHighlightedIndex((current) => {
      let next = edgeIndex ?? (isOpen ? current : selectedIndex);
      if (edgeIndex === null) next = (next + direction + options.length) % options.length;
      for (let checked = 0; checked < options.length; checked += 1) {
        if (!options[next]?.disabled) return next;
        next = (next + direction + options.length) % options.length;
      }
      return -1;
    });
  };

  return (
    <div ref={rootRef} className={`custom-select ${className}`}>
      <select
        {...nativeProps}
        ref={nativeRef}
        id={id ? `${id}-native` : undefined}
        name={name}
        value={selectedValue}
        onChange={(event) => {
          if (value === undefined) setLocalValue(event.target.value);
          onChange?.(event);
        }}
        disabled={disabled}
        className="custom-select-native"
        aria-hidden="true"
        tabIndex={-1}
      >
        {children}
      </select>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        className="custom-select-trigger"
        disabled={disabled}
        title={title}
        role="combobox"
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        aria-expanded={isOpen}
        aria-controls={listboxId}
        aria-haspopup="listbox"
        aria-activedescendant={isOpen && highlightedIndex >= 0 ? `${listboxId}-${highlightedIndex}` : undefined}
        onClick={() => (isOpen ? setIsOpen(false) : openMenu())}
        onKeyDown={handleKeyDown}
      >
        <span className="custom-select-value">{selectedOption?.label || selectedValue}</span>
        <ChevronDown className="custom-select-chevron" aria-hidden="true" />
      </button>
      {isOpen &&
        createPortal(
          <div
            ref={menuRef}
            id={listboxId}
            role="listbox"
            className="custom-select-menu"
            style={menuStyle}
            aria-label={ariaLabel}
          >
            {options.map((option, index) => {
              const isSelected = option.value === selectedValue;
              const isHighlighted = index === highlightedIndex;
              return (
                <button
                  key={`${option.value}-${index}`}
                  id={`${listboxId}-${index}`}
                  data-option-index={index}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  disabled={option.disabled}
                  tabIndex={-1}
                  className={`custom-select-option${isSelected ? ' is-selected' : ''}${
                    isHighlighted ? ' is-highlighted' : ''
                  }`}
                  onPointerEnter={() => setHighlightedIndex(index)}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => choose(option.value)}
                >
                  <span>{option.label}</span>
                  {isSelected && <Check aria-hidden="true" />}
                </button>
              );
            })}
          </div>,
          document.body
        )}
    </div>
  );
};
