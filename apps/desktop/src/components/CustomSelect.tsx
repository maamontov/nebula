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

  const options = useMemo<SelectOption[]>(
    () =>
      React.Children.toArray(children).flatMap((child) => {
        if (!isValidElement<React.OptionHTMLAttributes<HTMLOptionElement>>(child)) return [];
        const optionValue = String(child.props.value ?? '');
        const label =
          typeof child.props.children === 'string' || typeof child.props.children === 'number'
            ? String(child.props.children)
            : optionValue;
        return [{ value: optionValue, label, disabled: Boolean(child.props.disabled) }];
      }),
    [children]
  );

  const selectedValue = String(value ?? defaultValue ?? options[0]?.value ?? '');
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === selectedValue));
  const selectedOption = options[selectedIndex];

  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const menuHeight = Math.min(options.length * 38 + 10, 280);
    const roomBelow = window.innerHeight - rect.bottom;
    const openAbove = roomBelow < menuHeight + 12 && rect.top > roomBelow;
    setMenuStyle({
      position: 'fixed',
      left: rect.left,
      top: openAbove ? Math.max(8, rect.top - menuHeight - 6) : rect.bottom + 6,
      width: rect.width,
      maxHeight: menuHeight,
    });
  }, [options.length]);

  const openMenu = useCallback(() => {
    if (disabled) return;
    setHighlightedIndex(selectedIndex);
    setIsOpen(true);
  }, [disabled, selectedIndex]);

  const choose = useCallback(
    (nextValue: string) => {
      const nativeSelect = nativeRef.current;
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
    [onChange]
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

  const handleKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
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
    if (!isOpen) openMenu();
    const direction = event.key === 'ArrowUp' ? -1 : 1;
    const edgeIndex = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : null;
    setHighlightedIndex((current) => {
      let next = edgeIndex ?? Math.max(0, current);
      if (edgeIndex === null) next = (next + direction + options.length) % options.length;
      while (options[next]?.disabled && next !== current) {
        next = (next + direction + options.length) % options.length;
      }
      return next;
    });
  };

  return (
    <div ref={rootRef} className={`custom-select ${className}`}>
      <select
        ref={nativeRef}
        id={id ? `${id}-native` : undefined}
        name={name}
        value={selectedValue}
        onChange={onChange}
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
        aria-expanded={isOpen}
        aria-controls={listboxId}
        aria-haspopup="listbox"
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
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  disabled={option.disabled}
                  className={`custom-select-option${isSelected ? ' is-selected' : ''}${
                    isHighlighted ? ' is-highlighted' : ''
                  }`}
                  onPointerEnter={() => setHighlightedIndex(index)}
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
