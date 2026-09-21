import { SliderInputFormField } from '@/components/slider-input-form-field';
import { Form } from '@/components/ui/form';
import { zodResolver } from '@hookform/resolvers/zod';
import { memo } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { initialParentChildChunkerValues } from '../../constant/pipeline';
import { useFormValues } from '../../hooks/use-form-values';
import { useWatchFormChange } from '../../hooks/use-watch-form-change';
import { INextOperatorForm } from '../../interface';
import { FormWrapper } from '../components/form-wrapper';

const FormSchema = z.object({
  parent_token_size: z.number().int().positive(),
  child_token_size: z.number().int().positive(),
  min_child_token_size: z.number().int().positive(),
  child_overlap_percent: z.number().min(0).max(30),
});

const ParentChildChunkerForm = ({ node }: INextOperatorForm) => {
  const values = useFormValues(initialParentChildChunkerValues, node);
  const form = useForm<z.infer<typeof FormSchema>>({
    defaultValues: values,
    resolver: zodResolver(FormSchema),
    mode: 'onChange',
  });
  useWatchFormChange(node?.id, form);

  return (
    <Form {...form}>
      <FormWrapper>
        <SliderInputFormField
          name="parent_token_size"
          label="Parent token limit"
          min={256}
          max={2048}
        />
        <SliderInputFormField
          name="child_token_size"
          label="Child token target"
          min={100}
          max={512}
        />
        <SliderInputFormField
          name="min_child_token_size"
          label="Minimum child tokens"
          min={32}
          max={300}
        />
        <SliderInputFormField
          name="child_overlap_percent"
          label="Child overlap (%)"
          min={0}
          max={30}
        />
      </FormWrapper>
    </Form>
  );
};

export default memo(ParentChildChunkerForm);
