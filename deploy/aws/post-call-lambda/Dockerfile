FROM public.ecr.aws/lambda/python:3.12

COPY deploy/aws/post-call-lambda/requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY src ${LAMBDA_TASK_ROOT}/src
ENV PYTHONPATH="${LAMBDA_TASK_ROOT}/src"

CMD ["chiefvoice.agents.post_call.lambda_handler.handler"]
